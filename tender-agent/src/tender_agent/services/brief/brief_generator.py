"""Bid-brief generation: stored content → Claude → validated recommendation.

Inputs come from the content store (tender_document_content rows). We never
re-read files from disk here — that defeats the point of the content store.
If a tender has no content rows we surface a clear "fetch documents first"
state; if it has some but the prompt would exceed the budget we prioritise
ITT/instructions + quality-questionnaire + spec docs by filename keyword,
and truncate large spreadsheet dumps.

The output is a strict JSON object validated with pydantic. The brief LEADS
with recommendation + confidence + key_risks because that's what the human
reads first when triaging "should we bid?".
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.models import Tender, TenderBrief, TenderDocumentContent

from .content_store import ensure_content_extracted
from .llm_client import (
    DEFAULT_BRIEF_MODEL,
    BriefLLMClient,
    LLMCallError,
    LLMConfigError,
    LLMResponse,
)

logger = structlog.get_logger(__name__)


# Rough chars-per-token. Claude's actual ratio varies (3-4 for English prose),
# but we only need an upper-bound estimate for budgeting. 4 is conservative.
CHARS_PER_TOKEN = 4

# Default input-token budget. The actual context window is much larger, but
# keeping prompts trimmed lowers cost and latency. Override per call.
DEFAULT_MAX_INPUT_TOKENS = int(os.environ.get("BRIEF_MAX_INPUT_TOKENS", "150000"))

# Per-doc safety cap when truncating spreadsheet-like content. Anything
# beyond this is dropped (with a note in documents_considered).
SPREADSHEET_TRUNCATE_CHARS = 8000

# Filename keywords that bump a document's priority when budgeting. Order
# implies precedence: ITT/instructions first, then quality, then spec.
PRIORITY_KEYWORDS = [
    ("itt", "itt"),
    ("instructions", "itt"),
    ("invitation", "itt"),
    ("rfq", "itt"),
    ("rfp", "itt"),
    ("tender", "itt"),
    ("quality", "quality"),
    ("questionnaire", "quality"),
    ("sq", "quality"),  # selection questionnaire
    ("specification", "spec"),
    ("spec", "spec"),
    ("scope", "spec"),
    ("requirements", "spec"),
]
PRIORITY_RANK = {"itt": 0, "quality": 1, "spec": 2, "other": 3}


# --------------------------------------------------------------------------
# Brief schema (pydantic) — validated against the LLM's JSON output.
# --------------------------------------------------------------------------


Recommendation = Literal["bid", "no_bid", "conditional"]
Confidence = Literal["high", "medium", "low"]
Severity = Literal["high", "medium", "low"]


class KeyRisk(BaseModel):
    risk: str
    severity: Severity
    detail: str | None = None


class DeadlineInfo(BaseModel):
    date: str | None = None
    note: str | None = None


class ContractValueInfo(BaseModel):
    amount: str | None = None
    note: str | None = None


class ScoringCriterion(BaseModel):
    criterion: str
    weight: str | None = None


class ScoringInfo(BaseModel):
    summary: str | None = None
    criteria: list[ScoringCriterion] = Field(default_factory=list)


class BriefPayload(BaseModel):
    """The validated bid brief returned by the LLM. Leads with the
    recommendation and key risks — those are what the human reads first."""

    recommendation: Recommendation
    confidence: Confidence
    headline: str
    rationale: str
    key_risks: list[KeyRisk] = Field(default_factory=list)
    deadline: DeadlineInfo | None = None
    contract_value: ContractValueInfo | None = None
    mandatory_requirements: list[str] = Field(default_factory=list)
    scoring: ScoringInfo | None = None
    scope_summary: str | None = None
    notable_conditions: list[str] = Field(default_factory=list)
    missing_or_unclear: list[str] = Field(default_factory=list)

    @field_validator("recommendation", mode="before")
    @classmethod
    def _normalise_recommendation(cls, v: Any) -> Any:
        if isinstance(v, str):
            s = v.strip().lower().replace("-", "_").replace(" ", "_")
            if s in ("nobid", "no_bid"):
                return "no_bid"
            return s
        return v

    @field_validator("confidence", "key_risks", mode="before")
    @classmethod
    def _normalise_simple(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip().lower()
        return v


# --------------------------------------------------------------------------
# Prompt assembly
# --------------------------------------------------------------------------


SYSTEM_PROMPT = """You are an expert UK construction bid manager. You analyse \
the ITT pack for an SME contractor and produce a *2-page* bid-brief that \
leads with a clear BID / NO-BID / CONDITIONAL recommendation and a ranked \
list of key risks. You are sceptical, plain-spoken, and never invent facts.

Rules:
- Use ONLY information present in the supplied documents. If the documents do \
not say something, mark it in `missing_or_unclear`. Never invent dates, \
values, or requirements.
- For monetary amounts, deadlines, and quoted text, use the EXACT form from \
the source.
- The recommendation considers: SME-fit, scope-fit, deadline realism, value \
viability, mandatory pass/fail requirements, and contractual risk \
(bonds/retention/PCG/TUPE/unusual indemnities).
- Mark `confidence: low` when the docs are sparse or contradictory rather \
than guessing.
- Output a SINGLE strict JSON object matching the requested schema. No \
preamble, no markdown fences, no trailing commentary.
"""


SCHEMA_DESCRIPTION = """{
  "recommendation": "bid" | "no_bid" | "conditional",
  "confidence":     "high" | "medium" | "low",
  "headline":       string,                       // one sentence the human reads first
  "rationale":      string,                       // 2-4 sentences justifying the call
  "key_risks": [                                  // RANKED, highest concern first
    { "risk": string, "severity": "high"|"medium"|"low", "detail": string|null }
  ],
  "deadline":        { "date": string|null, "note": string|null },
  "contract_value":  { "amount": string|null, "note": string|null },
  "mandatory_requirements": [string],             // pass/fail items the buyer requires
  "scoring": {
    "summary":  string|null,                      // e.g. "70% price / 30% quality"
    "criteria": [ { "criterion": string, "weight": string|null } ]
  },
  "scope_summary":       string|null,             // 3-5 sentences, plain English
  "notable_conditions":  [string],                // bonds, retention, PCG, TUPE, etc.
  "missing_or_unclear":  [string]                 // what to clarify with the buyer
}"""


def _format_money(t: Tender) -> str:
    if t.value_amount is None:
        return "(not stated)"
    amount = (
        t.value_amount
        if isinstance(t.value_amount, Decimal)
        else Decimal(str(t.value_amount))
    )
    return f"{t.value_currency or ''} {amount}".strip()


def _format_deadline(t: Tender) -> str:
    return t.deadline_at.isoformat() if t.deadline_at else "(not stated)"


def _priority_category(filename: str | None) -> str:
    if not filename:
        return "other"
    name = filename.lower()
    for keyword, cat in PRIORITY_KEYWORDS:
        if keyword in name:
            return cat
    return "other"


def _looks_like_spreadsheet(content: TenderDocumentContent) -> bool:
    return content.doc_type in ("xlsx", "csv")


def _trim_to(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    cut = text[:max_chars].rsplit("\n", 1)[0]
    return cut + "\n[…truncated…]", True


@dataclass
class _DocSlot:
    content: TenderDocumentContent
    priority: int  # PRIORITY_RANK
    filename: str
    body: str  # the actual text to include (possibly trimmed)
    included: str  # full | truncated | omitted


def _filename_for(content: TenderDocumentContent) -> str:
    doc_file = content.document_file
    if doc_file and doc_file.title:
        return doc_file.title
    if content.doc_type == "zip-member":
        return "(zip member)"
    return f"document-{content.document_file_id}"


def _select_documents(
    contents: list[TenderDocumentContent],
    *,
    max_chars: int,
) -> tuple[list[_DocSlot], list[_DocSlot]]:
    """Pick which content rows make it into the prompt.

    Returns (included_slots_in_priority_order, all_slots_for_audit).
    The audit list includes omitted rows so the brief footer can show
    "based on N of M documents (X truncated)".
    """
    # Score every content row, even ones we won't include, so the audit
    # log records why each was kept/trimmed/dropped.
    all_slots: list[_DocSlot] = []
    for c in contents:
        filename = _filename_for(c)
        category = _priority_category(filename)
        priority = PRIORITY_RANK[category]
        # Skip rows with no extractable text — they go in the audit as
        # "omitted (no text)".
        if not c.extracted_text:
            all_slots.append(
                _DocSlot(
                    content=c,
                    priority=priority,
                    filename=filename,
                    body="",
                    included="omitted",
                )
            )
            continue
        body = c.extracted_text
        truncated = False
        if _looks_like_spreadsheet(c):
            body, truncated = _trim_to(body, SPREADSHEET_TRUNCATE_CHARS)
        all_slots.append(
            _DocSlot(
                content=c,
                priority=priority,
                filename=filename,
                body=body,
                included="truncated" if truncated else "full",
            )
        )

    # Stable sort by priority — highest-priority docs first.
    candidates = [s for s in all_slots if s.body]
    candidates.sort(key=lambda s: s.priority)

    # Fit them under the char budget. Anything that doesn't fit becomes
    # included="omitted" (we mutate the slot in `all_slots` since it's the
    # same object).
    #
    # Priority docs (ITT/instructions, quality, spec) are precious: if one
    # doesn't fit cleanly, we hard-truncate it rather than dropping it.
    # Non-priority "other" docs are skipped when they don't fit, so a single
    # oversized supplementary file can't crowd out everything else.
    budget = max_chars
    included: list[_DocSlot] = []
    for slot in candidates:
        header = f"\n\n=== {slot.filename} ===\n"
        chunk = header + slot.body
        if len(chunk) <= budget:
            included.append(slot)
            budget -= len(chunk)
            continue
        is_priority = slot.priority < PRIORITY_RANK["other"]
        if not is_priority:
            slot.included = "omitted"
            continue
        if budget < len(header) + 500:
            slot.included = "omitted"
            continue
        trimmed_body, _ = _trim_to(slot.body, budget - len(header))
        slot.body = trimmed_body
        slot.included = "truncated"
        included.append(slot)
        budget = 0

    omitted = [s for s in candidates if s not in included]
    for s in omitted:
        s.included = "omitted"
    return included, all_slots


# --------------------------------------------------------------------------
# Generator
# --------------------------------------------------------------------------


@dataclass
class GenerationOutcome:
    brief: TenderBrief
    payload: BriefPayload | None
    status: str  # complete | failed | no_documents


_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n?")


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = _FENCE_RE.sub("", text, count=1)
        if text.endswith("```"):
            text = text[: text.rfind("```")]
    return text.strip()


def _try_parse(raw: str) -> BriefPayload | None:
    cleaned = _strip_fences(raw)
    # Some LLMs occasionally wrap the object in surrounding prose despite
    # being told not to. Extract the outermost {...} as a fallback.
    try:
        return BriefPayload.model_validate_json(cleaned)
    except (ValidationError, ValueError, json.JSONDecodeError):
        pass
    try:
        start = cleaned.index("{")
        end = cleaned.rindex("}")
        return BriefPayload.model_validate_json(cleaned[start : end + 1])
    except (ValueError, ValidationError, json.JSONDecodeError):
        return None


def _build_user_prompt(
    tender: Tender, included_docs: list[_DocSlot]
) -> str:
    docs_block = "".join(
        f"\n\n=== {slot.filename} ===\n{slot.body}" for slot in included_docs
    )
    if not docs_block:
        docs_block = "(no document text available — base brief on tender metadata only)"

    return f"""Produce the bid brief for the following UK public tender.

TENDER METADATA
---------------
Title: {tender.title or "(untitled)"}
Buyer: {tender.buyer_name or "(unknown)"}
Value: {_format_money(tender)}
Deadline: {_format_deadline(tender)}
Source URL: {tender.source_url or "(none)"}
CPV codes: {", ".join(tender.cpv_codes or []) or "(none)"}

DOCUMENTS ({len(included_docs)} included)
---------
{docs_block}

OUTPUT
------
Return a single JSON object matching exactly this schema. No prose, no \
markdown fences, no commentary.

{SCHEMA_DESCRIPTION}
"""


def _record_documents_considered(all_slots: list[_DocSlot]) -> list[dict]:
    return [
        {
            "filename": s.filename,
            "doc_type": s.content.doc_type,
            "char_count": s.content.char_count or 0,
            "included": s.included,
            "extraction_status": s.content.extraction_status,
        }
        for s in all_slots
    ]


async def generate_brief(
    db: Session,
    tender: Tender,
    *,
    llm: BriefLLMClient,
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
) -> GenerationOutcome:
    """Generate and persist a TenderBrief for `tender`.

    Always inserts a new TenderBrief row (regenerate = another row). The
    dashboard reads the latest by created_at.
    """
    now = datetime.now(UTC)
    brief = TenderBrief(
        tender_id=tender.id,
        status="generating",
        created_at=now,
        updated_at=now,
    )
    db.add(brief)
    db.commit()
    db.refresh(brief)

    # Idempotent safety net — if the fetch flow already extracted, this is a
    # no-op fast path that just returns the existing content rows.
    ensure_content_extracted(db, tender)

    # Look up content rows for every sha256 this tender's files have. We
    # match by sha256 (not tender_id) because the content store keeps ONE
    # canonical extraction per (sha256, extractor_version) globally — a
    # second tender that points at the same document reuses the row owned
    # by the first tender that fetched it. This is the cross-tender reuse
    # path that lets us never re-extract identical content.
    file_shas = {f.sha256 for f in (tender.document_files or []) if f.sha256}
    contents: list[TenderDocumentContent] = []
    if file_shas:
        contents = list(
            db.execute(
                select(TenderDocumentContent)
                .where(TenderDocumentContent.sha256.in_(file_shas))
            ).scalars().all()
        )

    if not contents:
        brief.status = "no_documents"
        brief.error_detail = (
            "No documents to analyse — fetch documents first."
        )
        brief.updated_at = datetime.now(UTC)
        db.commit()
        logger.info("brief.no_documents", tender_id=tender.id)
        return GenerationOutcome(brief=brief, payload=None, status="no_documents")

    char_budget = max(1000, max_input_tokens * CHARS_PER_TOKEN)
    included, all_slots = _select_documents(contents, max_chars=char_budget)
    docs_considered = _record_documents_considered(all_slots)

    user_prompt = _build_user_prompt(tender, included)

    try:
        first: LLMResponse = await llm.generate_brief(
            system=SYSTEM_PROMPT,
            user=user_prompt,
        )
    except LLMConfigError as exc:
        brief.status = "failed"
        brief.error_detail = str(exc)
        brief.model = DEFAULT_BRIEF_MODEL
        brief.documents_considered = docs_considered
        brief.updated_at = datetime.now(UTC)
        db.commit()
        logger.warning("brief.config_error", tender_id=tender.id, error=str(exc))
        return GenerationOutcome(brief=brief, payload=None, status="failed")
    except LLMCallError as exc:
        brief.status = "failed"
        brief.error_detail = str(exc)
        brief.model = DEFAULT_BRIEF_MODEL
        brief.documents_considered = docs_considered
        brief.updated_at = datetime.now(UTC)
        db.commit()
        logger.warning("brief.llm_error", tender_id=tender.id, error=str(exc))
        return GenerationOutcome(brief=brief, payload=None, status="failed")

    parsed = _try_parse(first.text)
    used = first

    if parsed is None:
        # One stricter retry — ask the model to return *only* the JSON object.
        retry_user = (
            user_prompt
            + "\n\nThe previous response was not parseable JSON. Reply with "
            "ONLY the JSON object — no prose, no fences, no commentary."
        )
        try:
            second = await llm.generate_brief(
                system=SYSTEM_PROMPT,
                user=retry_user,
            )
        except Exception as exc:  # noqa: BLE001
            brief.status = "failed"
            brief.error_detail = f"LLM retry failed: {exc}"
            brief.model = used.model
            brief.input_tokens = used.input_tokens
            brief.output_tokens = used.output_tokens
            brief.documents_considered = docs_considered
            brief.updated_at = datetime.now(UTC)
            db.commit()
            logger.warning(
                "brief.retry_failed", tender_id=tender.id, error=str(exc)
            )
            return GenerationOutcome(brief=brief, payload=None, status="failed")
        parsed = _try_parse(second.text)
        used = second

    if parsed is None:
        brief.status = "failed"
        brief.error_detail = "LLM returned non-JSON content twice."
        brief.model = used.model
        brief.input_tokens = used.input_tokens
        brief.output_tokens = used.output_tokens
        brief.documents_considered = docs_considered
        brief.updated_at = datetime.now(UTC)
        db.commit()
        logger.warning("brief.parse_failed_twice", tender_id=tender.id)
        return GenerationOutcome(brief=brief, payload=None, status="failed")

    brief.status = "complete"
    brief.recommendation = parsed.recommendation
    brief.confidence = parsed.confidence
    brief.headline = parsed.headline
    brief.brief_json = parsed.model_dump(mode="json")
    brief.model = used.model
    brief.input_tokens = used.input_tokens
    brief.output_tokens = used.output_tokens
    brief.documents_considered = docs_considered
    brief.generated_at = datetime.now(UTC)
    brief.updated_at = datetime.now(UTC)
    brief.error_detail = None
    db.commit()
    db.refresh(brief)

    logger.info(
        "brief.generated",
        tender_id=tender.id,
        brief_id=brief.id,
        recommendation=brief.recommendation,
        confidence=brief.confidence,
        documents_included=sum(1 for s in all_slots if s.included != "omitted"),
        documents_total=len(all_slots),
        input_tokens=brief.input_tokens,
        output_tokens=brief.output_tokens,
        model=brief.model,
    )
    return GenerationOutcome(brief=brief, payload=parsed, status="complete")
