"""Per-tender sector classifier (Haiku 4.5) + persistence + worker pass.

One tender → one Anthropic call (cached taxonomy system prompt, low
temperature) → a validated `ClassificationResult` → stamped onto the tender.
Designed so a tender can be classified independently of requirements
extraction: it reads only title + description (+ buyer), never documents.

Robustness contract (Phase 3a):
- Strict JSON out; validated against the allowed taxonomy value lists.
- Invalid / garbled output → retry ONCE, then store "Other / Uncategorised"
  rather than fail the tender.
- Only canonical taxonomy strings are ever stored.
- A construction sub-category is set ONLY when Construction is primary or
  secondary; null otherwise.
- Token usage is logged on every call so real spend stays visible.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from tender_agent.config import settings
from tender_agent.models import Tender
from tender_agent.services.classification.taxonomy import (
    CLASSIFIER_VERSION,
    CONSTRUCTION_SECTOR,
    OTHER_SECTOR,
    build_user_text,
    canonical_sector,
    canonical_subcategory,
    system_prompt_blocks,
)

logger = structlog.get_logger(__name__)

#: Classification output is tiny (one JSON object) — keep max_tokens small.
_MAX_TOKENS = 512


@dataclass
class ClassificationResult:
    """A validated classification. `valid` is False when both LLM attempts
    failed and we fell back to Other (the tender is still classified, never
    left in an error state)."""

    primary_sector: str
    secondary_sectors: list[str] = field(default_factory=list)
    construction_subcategories: list[str] = field(default_factory=list)
    valid: bool = True
    input_tokens: int = 0
    output_tokens: int = 0


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[: text.rfind("```")]
    return text.strip()


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def parse_classification(raw_text: str) -> ClassificationResult | None:
    """Parse + validate one model response.

    Returns a valid `ClassificationResult`, or None when the output is
    unusable (unparseable JSON, or a primary_sector that isn't in the
    taxonomy) — None is the signal to retry / fall back. Invalid secondary or
    sub-category values are silently filtered (not a retry trigger): the
    primary is what must be trustworthy.
    """
    try:
        data = json.loads(_strip_fences(raw_text))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    primary = canonical_sector(data.get("primary_sector"))
    if primary is None:
        return None  # unusable primary → retry / fall back

    raw_secondary = data.get("secondary_sectors")
    secondary: list[str] = []
    if isinstance(raw_secondary, list):
        for value in raw_secondary:
            canon = canonical_sector(value if isinstance(value, str) else None)
            if canon is not None and canon != primary:
                secondary.append(canon)
    secondary = _dedupe(secondary)

    construction_relevant = (
        primary == CONSTRUCTION_SECTOR or CONSTRUCTION_SECTOR in secondary
    )
    subcategories: list[str] = []
    if construction_relevant:
        raw_subcats = data.get("construction_subcategories")
        if isinstance(raw_subcats, list):
            for value in raw_subcats:
                canon = canonical_subcategory(
                    value if isinstance(value, str) else None
                )
                if canon is not None:
                    subcategories.append(canon)
        subcategories = _dedupe(subcategories)

    return ClassificationResult(
        primary_sector=primary,
        secondary_sectors=secondary,
        construction_subcategories=subcategories,
        valid=True,
    )


def _usage(response) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0
    return (
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
    )


def _response_text(response) -> str:
    blocks = [
        b.text
        for b in getattr(response, "content", []) or []
        if getattr(b, "type", None) == "text"
    ]
    return "\n".join(blocks)


def _make_client():
    from anthropic import Anthropic  # noqa: PLC0415 — lazy: keeps import cost off hot paths

    return Anthropic(api_key=settings.anthropic_api_key)


def classify_text(
    title: str | None,
    description: str | None,
    buyer_name: str | None = None,
    *,
    client=None,
    model: str | None = None,
) -> ClassificationResult:
    """Classify one tender's text. Always returns a result — on two failed
    attempts it returns an `Other / Uncategorised` result with valid=False
    (never raises, never leaves the tender unclassified)."""
    client = client or _make_client()
    model = model or settings.classifier_model
    user_text = build_user_text(title, description, buyer_name)

    last_in = last_out = 0
    for attempt in range(2):  # initial try + one retry
        try:
            response = client.messages.create(
                model=model,
                max_tokens=_MAX_TOKENS,
                temperature=0,
                system=system_prompt_blocks(),
                messages=[{"role": "user", "content": user_text}],
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "classification.api_error", attempt=attempt, error=str(exc)
            )
            continue

        in_tok, out_tok = _usage(response)
        last_in, last_out = in_tok, out_tok
        logger.info(
            "classification.tokens",
            attempt=attempt,
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
        )
        result = parse_classification(_response_text(response))
        if result is not None:
            result.input_tokens = in_tok
            result.output_tokens = out_tok
            return result
        logger.warning("classification.invalid_output", attempt=attempt)

    logger.warning("classification.fallback_other")
    return ClassificationResult(
        primary_sector=OTHER_SECTOR,
        valid=False,
        input_tokens=last_in,
        output_tokens=last_out,
    )


def apply_classification(tender: Tender, result: ClassificationResult) -> None:
    """Stamp a classification onto a tender (does not commit). Subcategories
    are stored as a per-sector dict so other sectors' sub-lists can be added
    later without a schema change."""
    tender.primary_sector = result.primary_sector
    tender.secondary_sectors = list(result.secondary_sectors)
    if result.construction_subcategories:
        tender.subcategories = {
            CONSTRUCTION_SECTOR: list(result.construction_subcategories)
        }
    else:
        tender.subcategories = None
    tender.classified_at = datetime.now(UTC)
    tender.classifier_version = CLASSIFIER_VERSION


def classify_and_store(
    db: Session,
    tender: Tender,
    *,
    client=None,
    force: bool = False,
) -> ClassificationResult | None:
    """Classify a single tender and persist the result. Idempotent: a tender
    already on the current `CLASSIFIER_VERSION` is skipped unless `force`.

    Returns None when skipped (already current) or when classification can't
    run (no API key and no injected client)."""
    if not force and tender.classifier_version == CLASSIFIER_VERSION:
        return None
    if client is None and not settings.anthropic_api_key:
        logger.warning("classification.skipped_no_api_key", tender_id=tender.id)
        return None

    result = classify_text(
        tender.title, tender.description, tender.buyer_name, client=client
    )
    apply_classification(tender, result)
    db.commit()
    logger.info(
        "classification.stored",
        tender_id=tender.id,
        primary_sector=result.primary_sector,
        secondary_count=len(result.secondary_sectors),
        subcategory_count=len(result.construction_subcategories),
        valid=result.valid,
        version=CLASSIFIER_VERSION,
    )
    return result


def pending_classification(db: Session, limit: int) -> list[Tender]:
    """Non-duplicate tenders not yet on the current classifier version,
    oldest id first (stable FIFO so a backlog drains predictably)."""
    stmt = (
        select(Tender)
        .where(Tender.duplicate_of_id.is_(None))
        .where(
            or_(
                Tender.classifier_version.is_(None),
                Tender.classifier_version != CLASSIFIER_VERSION,
            )
        )
        .order_by(Tender.id.asc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().all())


def process_pending_classification(
    db: Session, *, limit: int, client=None
) -> int:
    """Classify up to `limit` not-yet-current tenders. Returns the count
    processed. One bad tender is logged and skipped, never fatal."""
    pending = pending_classification(db, limit)
    if not pending:
        logger.info("classification_worker.empty", limit=limit)
        return 0
    logger.info(
        "classification_worker.starting",
        batch_size=len(pending),
        tender_ids=[t.id for t in pending],
    )
    processed = 0
    for tender in pending:
        try:
            classify_and_store(db, tender, client=client)
            processed += 1
        except Exception:  # noqa: BLE001
            logger.exception(
                "classification_worker.tender_failed", tender_id=tender.id
            )
            db.rollback()
    logger.info("classification_worker.finished", processed=processed)
    return processed
