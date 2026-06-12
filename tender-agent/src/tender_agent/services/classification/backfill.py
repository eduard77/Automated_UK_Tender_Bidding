"""One-time classification backfill over the existing unclassified pool.

Uses the Anthropic **Message Batches API** (≈50% cheaper than per-call, and
the backlog isn't time-sensitive). Submits one request per tender, polls the
batch to completion, then writes each result back onto its tender.

Bounded + resumable + idempotent by construction:
- Bounded by `limit` (one submission of at most `limit` tenders).
- Resumable: the watermark is `Tender.classifier_version`. A re-run selects
  only tenders not yet on the current version, so re-running after a partial
  pass (or after a version bump) picks up exactly what's left — no separate
  tracking table.
- Idempotent: a tender already on the current version is never re-submitted.

The client and the sleep function are injectable so the whole flow is
testable offline against a fake batch client.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import structlog

from tender_agent.config import settings
from tender_agent.db import SessionLocal
from tender_agent.models import Tender
from tender_agent.services.classification.classifier import (
    _MAX_TOKENS,
    ClassificationResult,
    _make_client,
    _response_text,
    _usage,
    apply_classification,
    parse_classification,
    pending_classification,
)
from tender_agent.services.classification.taxonomy import (
    OTHER_SECTOR,
    build_user_text,
    system_prompt_blocks,
)

logger = structlog.get_logger(__name__)


@dataclass
class BackfillSummary:
    submitted: int = 0
    classified: int = 0
    errored: int = 0
    skipped_no_tender: int = 0
    batch_id: str | None = None
    #: "ok" (submitted + collected) | "empty" (nothing to do) |
    #: "pending" (batch still processing past max_wait — re-collect later) |
    #: "no_api_key"
    status: str = "ok"
    input_tokens: int = 0
    output_tokens: int = 0


def _custom_id(tender_id: int) -> str:
    return f"tender-{tender_id}"


def _tender_id_from_custom_id(custom_id: str) -> int | None:
    if not custom_id or not custom_id.startswith("tender-"):
        return None
    try:
        return int(custom_id.split("-", 1)[1])
    except (ValueError, IndexError):
        return None


def build_batch_request(tender: Tender) -> dict:
    """One Batch-API request for one tender — same model/prompt/params as the
    per-call path (so caching applies across the batch too)."""
    return {
        "custom_id": _custom_id(tender.id),
        "params": {
            "model": settings.classifier_model,
            "max_tokens": _MAX_TOKENS,
            "temperature": 0,
            "system": system_prompt_blocks(),
            "messages": [
                {
                    "role": "user",
                    "content": build_user_text(
                        tender.title, tender.description, tender.buyer_name
                    ),
                }
            ],
        },
    }


def collect_results(db_factory, batch_id: str, *, client=None) -> BackfillSummary:
    """Stream an ENDED batch's results and write each classification back.

    Separated from `run_backfill` so a `pending` batch can be collected later
    (same batch_id) without resubmitting."""
    client = client or _make_client()
    summary = BackfillSummary(batch_id=batch_id)
    with db_factory() as db:
        for entry in client.messages.batches.results(batch_id):
            tender_id = _tender_id_from_custom_id(getattr(entry, "custom_id", ""))
            if tender_id is None:
                continue
            tender = db.get(Tender, tender_id)
            if tender is None:
                summary.skipped_no_tender += 1
                continue
            result = getattr(entry, "result", None)
            if getattr(result, "type", None) != "succeeded":
                summary.errored += 1
                logger.warning(
                    "classification.backfill_result_errored",
                    tender_id=tender_id,
                    result_type=getattr(result, "type", None),
                )
                continue
            message = getattr(result, "message", None)
            in_tok, out_tok = _usage(message)
            summary.input_tokens += in_tok
            summary.output_tokens += out_tok
            parsed = parse_classification(_response_text(message))
            if parsed is None:
                # Garbled batch output → Other (don't fail the tender).
                parsed = ClassificationResult(
                    primary_sector=OTHER_SECTOR, valid=False
                )
            apply_classification(tender, parsed)
            db.commit()
            summary.classified += 1
    return summary


def run_backfill(
    db_factory=SessionLocal,
    *,
    limit: int,
    client=None,
    poll_interval_s: float = 5.0,
    max_wait_s: float = 1800.0,
    sleep=time.sleep,
) -> BackfillSummary:
    """Submit one batch of up to `limit` unclassified tenders, poll to
    completion, and write results back. Safe to re-run (watermark resumable).
    """
    if client is None and not settings.anthropic_api_key:
        logger.warning("classification.backfill_no_api_key")
        return BackfillSummary(status="no_api_key")
    client = client or _make_client()

    with db_factory() as db:
        pending = pending_classification(db, limit)
        requests = [build_batch_request(t) for t in pending]
        tender_ids = [t.id for t in pending]

    if not requests:
        logger.info("classification.backfill_empty")
        return BackfillSummary(status="empty")

    batch = client.messages.batches.create(requests=requests)
    batch_id = getattr(batch, "id", None)
    logger.info(
        "classification.backfill_submitted",
        batch_id=batch_id,
        count=len(requests),
        tender_ids=tender_ids[:20],
    )
    summary = BackfillSummary(submitted=len(requests), batch_id=batch_id)

    status = getattr(batch, "processing_status", None)
    max_polls = max(1, int(max_wait_s / poll_interval_s)) if poll_interval_s > 0 else 1
    polls = 0
    while status != "ended" and polls < max_polls:
        sleep(poll_interval_s)
        polls += 1
        refreshed = client.messages.batches.retrieve(batch_id)
        status = getattr(refreshed, "processing_status", None)

    if status != "ended":
        logger.warning(
            "classification.backfill_pending", batch_id=batch_id, status=status
        )
        summary.status = "pending"
        return summary

    collected = collect_results(db_factory, batch_id, client=client)
    summary.classified = collected.classified
    summary.errored = collected.errored
    summary.skipped_no_tender = collected.skipped_no_tender
    summary.input_tokens = collected.input_tokens
    summary.output_tokens = collected.output_tokens
    logger.info(
        "classification.backfill_complete",
        batch_id=batch_id,
        submitted=summary.submitted,
        classified=summary.classified,
        errored=summary.errored,
        input_tokens=summary.input_tokens,
        output_tokens=summary.output_tokens,
    )
    return summary
