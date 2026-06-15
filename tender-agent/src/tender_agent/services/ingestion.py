"""Orchestrates a full poll-and-ingest cycle for one source."""
from __future__ import annotations

import asyncio
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
from tender_agent.services.portal_classifier import schedule_classification
from tender_agent.services.portal_discovery import process_tender_for_portals
from tender_agent.services.regions import resolve_for_tender

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

    # Canonical region (chunk 8): extracted from the raw OCDS delivery-address
    # data (region -> postcode -> country). Deterministic only in the polling
    # hot path — never an AI call; the rare unresolved tenders become
    # "Unspecified" and the backfill can upgrade them later. Source-agnostic.
    region = resolve_for_tender(
        raw_payload, normalised.buyer_region, normalised.buyer_country
    )

    if existing is None:
        tender = Tender(
            **payload,
            documents=docs_payload,
            raw=raw_payload,
            region=region,
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
    existing.region = region
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
    adapter = None

    try:
        adapter = adapter_cls()

        def _persist_progress() -> None:
            """Advance the durable resume point from the adapter's
            confirmed-page watermark and COMMIT it now — called as each page
            confirms.

            This is what makes a backlog catch-up resumable. The 900s
            per-source poll timeout cancels poll_source with a
            ``CancelledError`` — a BaseException, so it bypasses the
            ``except Exception`` below AND the final finalisation/commit.
            Unless progress is already committed when the cancel lands, the
            whole sweep is discarded and the next run restarts from the
            backlog start: the CF/FTS 900s non-advancing loop. Committing the
            watermark per confirmed page means a cancelled mid-sweep run keeps
            everything fetched so far and the next run resumes from here.
            Monotonic — never moves the watermark backwards."""
            if adapter is None:
                return
            progress = getattr(adapter, "progress_watermark", None)
            if progress is None:
                return
            current = source.last_polled_at
            if current is not None and current.tzinfo is None:
                current = current.replace(tzinfo=UTC)
            if current is not None and progress <= current:
                return
            source.last_polled_at = progress
            run.watermark_at = progress
            run.fetched = fetched
            run.new_count = new_count
            run.updated_count = updated_count
            db.commit()
            logger.info(
                "ingest.watermark_advance",
                source=source.code,
                watermark=progress.isoformat(),
                fetched=fetched,
            )

        async with adapter:
            try:
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
                    # Dispatch push notifications for new matches immediately.
                    # Document download + Anthropic requirements extraction used
                    # to run HERE in-line; the live 2026-06-11 Log stream showed
                    # that consumed the whole poll interval on FTS alone and
                    # starved late-list sources (EU_SUPPLY/PROACTIS/ATAMIS) from
                    # ever being reached. Enrichment now happens out-of-band in
                    # services.enrichment_worker.process_pending_enrichment, on
                    # its own scheduler interval. Push is best-effort; failures
                    # are logged in services/push and never raised, so a dead
                    # subscriber can't break ingestion.
                    if matched_profile_ids:
                        push.send_match_notifications(db, tender, matched_profile_ids)
                        db.commit()
                        logger.info(
                            "ingest.enrichment_deferred",
                            tender_id=tender.id,
                            match_count=len(matched_profile_ids),
                        )
                    # Resume point: advance + commit the watermark as pages
                    # confirm, so a 900s-timeout cancellation keeps forward
                    # progress and the next run resumes here, not from the backlog
                    # start (the CF/FTS non-advancing loop, 2026-06-14).
                    _persist_progress()
            except asyncio.CancelledError:
                # The 900s poll timeout cancels poll_source with a
                # ``CancelledError`` (a BaseException) at the page-boundary
                # ``await`` inside fetch_since — AFTER the adapter has set the
                # just-confirmed page's watermark but BEFORE the loop body's
                # per-record ``_persist_progress`` runs again. The cancel lands
                # BETWEEN records (in the next page's fetch), so the session is
                # clean and only the watermark is pending — commit it here so
                # the resume point survives the timeout, then re-raise so the
                # scheduler's wait_for still observes the cancellation. This is
                # the exact path the original #131 bug skipped: ``except
                # Exception`` cannot catch a BaseException. Defensive: a stray
                # error persisting must not mask the CancelledError.
                try:
                    _persist_progress()
                except Exception:  # noqa: BLE001
                    logger.exception("ingest.final_persist_failed", source=source.code)
                raise
            had_errors = bool(getattr(adapter, "had_errors", False))
            adapter_errors = list(getattr(adapter, "error_messages", []) or [])
        # A graceful end (clean completion OR an adapter-reported error such as
        # the CF 429 abort / FTS salvage) — capture the final confirmed page's
        # progress too. (A timeout CANCELLATION is handled by the except
        # CancelledError above.)
        _persist_progress()
        if had_errors:
            run.status = "error"
            # NEW (Phase-1 continuation, 2026-06-11): surface the REAL
            # upstream cause that the sources-health endpoint reads back.
            # The legacy generic message was the only thing the operator
            # ever saw, hiding 403-vs-500-vs-DNS behind one string.
            if adapter_errors:
                error = " | ".join(adapter_errors[-3:])
            else:
                error = "upstream HTTP requests failed (see adapter log events)"
        else:
            # Reached present day — the resume point IS now.
            source.last_polled_at = datetime.now(UTC)
            run.watermark_at = source.last_polled_at
            run.status = "ok"
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        run.status = "error"
        logger.exception("ingest.failed", source=source.code)
        db.rollback()
        # The incremental commits above already persisted the resume point
        # durably (rollback only discards the in-flight record), so a graceful
        # failure keeps its progress without any extra work here.

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
