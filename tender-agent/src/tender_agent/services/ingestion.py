"""Orchestrates a full poll-and-ingest cycle for one source."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.adapters import ADAPTERS
from tender_agent.config import settings
from tender_agent.models import FilterMatch, PollRun, Source, Tender
from tender_agent.schemas import NormalisedTender
from tender_agent.services import filter_engine, push
from tender_agent.services.deduplicator import find_duplicate
from tender_agent.services.document_downloader import download_documents_for_tender
from tender_agent.services.portal_classifier import schedule_classification
from tender_agent.services.portal_discovery import process_tender_for_portals
from tender_agent.services.requirements_extractor import extract_requirements

logger = structlog.get_logger(__name__)


def _content_hash(t: NormalisedTender) -> str:
    payload = {
        "title": t.title,
        "description": t.description,
        "value": str(t.value_amount) if t.value_amount is not None else None,
        "deadline": t.deadline_at.isoformat() if t.deadline_at else None,
        "status": t.status,
        "documents": [d.url for d in t.documents],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _upsert_tender(db: Session, normalised: NormalisedTender) -> tuple[Tender, str]:
    """Insert or update a tender. Returns (tender, action) where action is
    'new', 'updated', or 'unchanged'."""
    now = datetime.now(UTC)
    chash = _content_hash(normalised)

    existing = db.execute(
        select(Tender).where(
            Tender.source_code == normalised.source_code,
            Tender.source_ref == normalised.source_ref,
        )
    ).scalar_one_or_none()

    payload = normalised.model_dump(mode="json")
    docs_payload = payload.pop("documents", [])
    raw_payload = payload.pop("raw", {})

    if existing is None:
        tender = Tender(
            **payload,
            documents=docs_payload,
            raw=raw_payload,
            content_hash=chash,
            first_seen_at=now,
            last_seen_at=now,
        )
        # Deduplicate against other sources
        dup = find_duplicate(db, normalised)
        if dup is not None:
            tender.duplicate_of_id = dup.id
        db.add(tender)
        db.flush()
        return tender, "new"

    existing.last_seen_at = now
    if existing.content_hash == chash:
        return existing, "unchanged"

    # Apply update
    for k, v in payload.items():
        setattr(existing, k, v)
    existing.documents = docs_payload
    existing.raw = raw_payload
    existing.content_hash = chash
    return existing, "updated"


def _record_filter_matches(db: Session, tender: Tender) -> list[int]:
    """Match tender against enabled filter profiles.

    Returns the list of filter_profile_id values for which a NEW FilterMatch was
    just created (i.e. caller will use this to dispatch one notification per new
    match). An empty list means nothing new.
    """
    if tender.duplicate_of_id is not None:
        return []  # don't double-alert on duplicates
    profiles = filter_engine.matching_profiles(db, tender)
    new_profile_ids: list[int] = []
    for profile in profiles:
        existing = db.execute(
            select(FilterMatch).where(
                FilterMatch.tender_id == tender.id,
                FilterMatch.filter_profile_id == profile.id,
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(FilterMatch(tender_id=tender.id, filter_profile_id=profile.id, score=1.0))
            new_profile_ids.append(profile.id)
    return new_profile_ids


async def _enrich_matched_tender(db: Session, tender: Tender) -> None:
    """Download docs and extract requirements for a matched tender. Best-effort."""
    try:
        await download_documents_for_tender(db, tender)
    except Exception:  # noqa: BLE001
        logger.exception("enrich.download_failed", tender_id=tender.id)
    try:
        # Refresh document_files relationship
        db.refresh(tender)
        extract_requirements(db, tender)
    except Exception:  # noqa: BLE001
        logger.exception("enrich.extract_failed", tender_id=tender.id)


async def poll_source(db: Session, source: Source) -> PollRun:
    """Run one polling cycle for the given source."""
    adapter_cls = ADAPTERS.get(source.code)
    if adapter_cls is None:
        raise ValueError(f"No adapter registered for source code {source.code}")

    since = source.last_polled_at or (
        datetime.now(UTC) - timedelta(days=settings.lookback_days_initial)
    )
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)

    run = PollRun(source_id=source.id, status="running")
    db.add(run)
    db.flush()

    fetched = new_count = updated_count = 0
    error: str | None = None
    had_errors = False

    try:
        async with adapter_cls() as adapter:
            async for normalised in adapter.fetch_since(since):
                fetched += 1
                tender, action = _upsert_tender(db, normalised)
                matched_profile_ids: list[int] = []
                if action == "new":
                    new_count += 1
                    matched_profile_ids = _record_filter_matches(db, tender)
                elif action == "updated":
                    updated_count += 1
                    matched_profile_ids = _record_filter_matches(db, tender)
                # Best-effort portal discovery; never blocks the tender upsert.
                queued_portal_ids: list[int] = []
                try:
                    result = process_tender_for_portals(tender, db)
                    queued_portal_ids = result.portal_ids_queued
                except Exception:  # noqa: BLE001
                    logger.warning("portal_discovery.failed", tender_id=tender.id)
                # commit per record so a later failure doesn't lose progress
                db.commit()
                for portal_id in queued_portal_ids:
                    schedule_classification(portal_id)
                # Enrich matched tenders with documents + requirements, then dispatch
                # push notifications for the newly created matches. Push is
                # best-effort; failures are logged in services/push and never
                # raised, so a dead subscriber can't break ingestion.
                if matched_profile_ids:
                    try:
                        await _enrich_matched_tender(db, tender)
                    except Exception:  # noqa: BLE001
                        logger.exception("ingest.enrich_failed", tender_id=tender.id)
                    push.send_match_notifications(db, tender, matched_profile_ids)
                    db.commit()
            had_errors = bool(getattr(adapter, "had_errors", False))
        if had_errors:
            run.status = "error"
            error = "upstream HTTP requests failed (see adapter log events)"
        else:
            source.last_polled_at = datetime.now(UTC)
            run.status = "ok"
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        run.status = "error"
        logger.exception("ingest.failed", source=source.code)
        db.rollback()

    run.fetched = fetched
    run.new_count = new_count
    run.updated_count = updated_count
    run.error = error
    run.finished_at = datetime.now(UTC)
    db.commit()

    logger.info(
        "ingest.complete",
        source=source.code,
        fetched=fetched,
        new=new_count,
        updated=updated_count,
        status=run.status,
    )
    return run
