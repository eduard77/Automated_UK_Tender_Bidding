"""Background enrichment worker.

Phase-1 rev 3 (2026-06-11). The polling cycle used to enrich matched
tenders IN-LINE: for every new FilterMatch it downloaded documents and
ran Anthropic requirements extraction (~10–15 s per tender) before
moving on to the next yielded tender — and only AFTER finishing one
source did it start the next. The live Log stream showed FTS alone
emitting a long unbroken series of `requirements.extracted` events,
consuming the entire poll interval; sources at the tail of the list
(EU_SUPPLY, PROACTIS, ATAMIS) were never reached — hence the
permanent `never_polled` diagnosis even though they were registered.

This module re-homes the slow work. The poll cycle now only fetches
and upserts tenders + records FilterMatches + fires push
notifications; this worker — run on its own scheduler interval — picks
up matched tenders that don't yet have a TenderRequirements row and
runs the same enrichment in a bounded batch.

The implicit queue is `matched-but-not-yet-enriched`:

    Tender JOIN FilterMatch LEFT JOIN TenderRequirements
    WHERE TenderRequirements.id IS NULL
      AND Tender.duplicate_of_id IS NULL

No schema migration is required: the existence of the unique
TenderRequirements.tender_id row is the "enrichment done" marker, and
the unique index makes the worker idempotent — a concurrent poll +
worker can never double-insert.
"""
from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.models import FilterMatch, Tender, TenderRequirements
from tender_agent.services.document_downloader import download_documents_for_tender
from tender_agent.services.requirements_extractor import extract_requirements

logger = structlog.get_logger(__name__)


async def enrich_tender(db: Session, tender: Tender) -> None:
    """Download documents and extract requirements for a single tender.

    Best-effort and never raises: the two halves are independently
    wrapped so a flaky download doesn't skip extraction (and vice
    versa). Identical body to the old `_enrich_matched_tender` in
    ingestion.py before rev 3 moved it here.
    """
    try:
        await download_documents_for_tender(db, tender)
    except Exception:  # noqa: BLE001
        logger.exception("enrich.download_failed", tender_id=tender.id)
    try:
        db.refresh(tender)
        extract_requirements(db, tender)
    except Exception:  # noqa: BLE001
        logger.exception("enrich.extract_failed", tender_id=tender.id)


def _pending_tenders(db: Session, limit: int) -> list[Tender]:
    """Tenders with at least one FilterMatch and no TenderRequirements row.

    Ordered by tender id so the FIFO contract is stable across cycles —
    a tender created earlier never gets starved by a constant trickle
    of newer ones. Duplicates are excluded (`_record_filter_matches`
    skips them anyway, but the predicate stays here in case a
    duplicate slipped through historically).
    """
    stmt = (
        select(Tender)
        .join(FilterMatch, FilterMatch.tender_id == Tender.id)
        .outerjoin(TenderRequirements, TenderRequirements.tender_id == Tender.id)
        .where(TenderRequirements.id.is_(None))
        .where(Tender.duplicate_of_id.is_(None))
        .order_by(Tender.id.asc())
        .limit(limit)
        .distinct()
    )
    return list(db.execute(stmt).scalars().unique().all())


async def process_pending_enrichment(db: Session, *, limit: int) -> int:
    """Enrich up to `limit` pending tenders. Returns the number processed.

    Sequential within a batch on purpose: extraction is an Anthropic
    API call, and the operator's spend ceiling lives at the call site,
    not in a fan-out. The scheduler interval bounds throughput across
    cycles.
    """
    pending = _pending_tenders(db, limit)
    if not pending:
        logger.info("enrichment_worker.empty", limit=limit)
        return 0
    logger.info(
        "enrichment_worker.starting",
        batch_size=len(pending),
        tender_ids=[t.id for t in pending],
    )
    processed = 0
    for tender in pending:
        await enrich_tender(db, tender)
        processed += 1
    logger.info("enrichment_worker.finished", processed=processed)
    return processed
