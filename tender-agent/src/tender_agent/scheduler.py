"""Polling scheduler. Drives source ingestion at the configured interval."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select, update

from tender_agent.adapters import ADAPTERS
from tender_agent.config import settings
from tender_agent.db import SessionLocal
from tender_agent.models import PollRun, Source
from tender_agent.services.discovery.proactis_discovery import (
    run as run_proactis_discovery,
)
from tender_agent.services.discovery.proactis_filter_config import (
    ProactisFilterConfig,
)
from tender_agent.services.email.poller import poll_all_mailboxes
from tender_agent.services.enrichment_worker import process_pending_enrichment
from tender_agent.services.ingestion import poll_source

#: Mark `running` PollRuns as orphaned after this long without a finish.
#: A successful HTTP source finishes in seconds; the slowest (Proactis
#: browser discovery) finishes in single-digit minutes. 90 minutes
#: comfortably exceeds even a stuck Proactis cycle without false-positives.
ORPHAN_POLL_RUN_AFTER = timedelta(minutes=90)

logger = structlog.get_logger(__name__)
_scheduler: AsyncIOScheduler | None = None


async def proactis_discovery_job() -> None:
    """Background Proactis discovery cycle. Constructs the filter config from
    settings (Step 1) and runs once. Any uncaught exception is logged here so
    the APScheduler job doesn't trip-line the whole job queue."""
    if not settings.proactis_discovery_enabled:
        return
    config = ProactisFilterConfig(
        keywords=settings.proactis_discovery_keywords,
        regions=list(settings.proactis_discovery_regions),
        categories=list(settings.proactis_discovery_categories),
        portals=list(settings.proactis_discovery_portals),
        organisations=list(settings.proactis_discovery_organisations),
        include_closed=settings.proactis_discovery_include_closed,
        max_pages=settings.proactis_discovery_max_pages,
    )
    try:
        await run_proactis_discovery(config=config)
    except Exception:  # noqa: BLE001
        logger.exception("scheduler.proactis_discovery_failed")


async def enrichment_worker_job() -> None:
    """Background per-tender enrichment cycle (Phase-1 rev 3, 2026-06-11).

    Picks up matched-but-unenriched tenders from the implicit queue
    and runs PDF download + Anthropic requirements extraction off the
    polling hot path. Bounded by `enrichment_worker_batch_size` so a
    cycle terminates in predictable time even when a backlog has
    built up. Exceptions are logged and swallowed so a bad cycle can't
    trip-line the job queue."""
    if not settings.enrichment_worker_enabled:
        return
    try:
        with SessionLocal() as db:
            await process_pending_enrichment(
                db, limit=settings.enrichment_worker_batch_size
            )
    except Exception:  # noqa: BLE001
        logger.exception("scheduler.enrichment_worker_failed")


async def email_poll_job() -> None:
    """Background per-inbox email poll. Watches every connected mailbox for
    tender emails (exact subject reference), files them, drafts a reply, and
    notifies. Read-only and idempotent; never sends. Exceptions are logged here
    so a bad cycle can't trip-line the job queue."""
    if not settings.email_poll_enabled:
        return
    try:
        await poll_all_mailboxes()
    except Exception:  # noqa: BLE001
        logger.exception("scheduler.email_poll_failed")


def ensure_sources() -> None:
    """Make sure every registered adapter has a matching Source row."""
    with SessionLocal() as db:
        for code, adapter_cls in ADAPTERS.items():
            existing = db.execute(
                select(Source).where(Source.code == code)
            ).scalar_one_or_none()
            if existing is None:
                db.add(
                    Source(
                        code=code,
                        name=adapter_cls.name,
                        base_url=adapter_cls.base_url,
                        enabled=True,
                    )
                )
        db.commit()


def _orphan_stale_running_runs(db) -> int:
    """Mark PollRuns left in `running` past the orphan deadline as errored.

    A worker restart mid-poll, or a Proactis cycle that hung past the
    bridge timeout, leaves rows reading "running" indefinitely — they
    skew the sources-health diagnosis and the operator's "is the
    scheduler stuck?" question. Best-effort cleanup at the start of every
    poll cycle. Never raises (logged-and-swallowed)."""
    cutoff = datetime.now(UTC) - ORPHAN_POLL_RUN_AFTER
    try:
        result = db.execute(
            update(PollRun)
            .where(PollRun.status == "running")
            .where(PollRun.started_at < cutoff)
            .values(
                status="error",
                error=f"orphaned (no finished_at after {cutoff.isoformat()})",
                finished_at=datetime.now(UTC),
            )
        )
        db.commit()
        return int(getattr(result, "rowcount", 0) or 0)
    except Exception:  # noqa: BLE001
        logger.exception("scheduler.orphan_cleanup_failed")
        return 0


async def poll_all() -> None:
    """Poll every enabled source sequentially.

    Self-heals scheduling at the top of every cycle (`ensure_sources()`):
    a new adapter wired in after the previous boot gets its Source row
    here, so it joins the very next cycle instead of waiting for a
    redeploy. The per-iteration `scheduler.poll_source_attempt` log line
    makes the "did we even try to poll X?" question answerable from the
    Azure Log stream — without it, a source skipped by the iteration was
    indistinguishable from one whose poll finished cleanly with zero rows.
    """
    # Self-heal: ensure every registered adapter has a Source row at the
    # start of every cycle. Idempotent and cheap; covers the boot-order
    # case where ensure_sources() ran before a new adapter was registered.
    ensure_sources()
    with SessionLocal() as db:
        orphaned = _orphan_stale_running_runs(db)
        if orphaned:
            logger.info(
                "scheduler.poll_runs_orphaned",
                count=orphaned,
                cutoff_minutes=ORPHAN_POLL_RUN_AFTER.total_seconds() // 60,
            )
        sources = (
            db.execute(
                select(Source)
                .where(Source.enabled.is_(True))
                .order_by(Source.id)
            )
            .scalars()
            .all()
        )
        registered = list(ADAPTERS)
        logger.info(
            "scheduler.poll_all_starting",
            source_count=len(sources),
            source_codes=[s.code for s in sources],
            registered_adapters=registered,
        )
        for source in sources:
            adapter_in_registry = source.code in ADAPTERS
            logger.info(
                "scheduler.poll_source_attempt",
                source=source.code,
                has_adapter=adapter_in_registry,
            )
            if not adapter_in_registry:
                # Source rows for browser-driven flows (PROACTIS) live on
                # their own job; skip them here without raising, so the
                # iteration always reaches the HTTP adapters below.
                continue
            try:
                await poll_source(db, source)
            except Exception:  # noqa: BLE001
                logger.exception("scheduler.source_failed", source=source.code)


def start() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    ensure_sources()
    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(
        poll_all,
        trigger=IntervalTrigger(minutes=settings.poll_interval_minutes),
        id="poll_all",
        next_run_time=None,  # don't run immediately on startup; trigger via API or first interval
        coalesce=True,
        max_instances=1,
    )
    # Proactis discovery — runs on its own interval, only when enabled.
    # Browser-driven and slower than HTTP polling; runs in the same
    # AsyncIOScheduler so we get the same coalesce / max_instances safety.
    if settings.proactis_discovery_enabled:
        _scheduler.add_job(
            proactis_discovery_job,
            trigger=IntervalTrigger(
                minutes=settings.proactis_discovery_interval_minutes
            ),
            id="proactis_discovery",
            next_run_time=None,
            coalesce=True,
            max_instances=1,
        )
        logger.info(
            "scheduler.proactis_discovery_scheduled",
            interval_minutes=settings.proactis_discovery_interval_minutes,
        )
    # Email inbox poll — runs on its own interval, only when enabled. Same
    # coalesce / max_instances=1 safety so two cycles never overlap on one
    # mailbox (idempotency also guards this, but belt-and-braces).
    if settings.email_poll_enabled:
        _scheduler.add_job(
            email_poll_job,
            trigger=IntervalTrigger(
                minutes=settings.email_poll_interval_minutes
            ),
            id="email_poll",
            next_run_time=None,
            coalesce=True,
            max_instances=1,
        )
        logger.info(
            "scheduler.email_poll_scheduled",
            interval_minutes=settings.email_poll_interval_minutes,
        )
    # Enrichment worker — Phase-1 rev 3 (2026-06-11). Runs on its own
    # interval so the polling hot path is never blocked behind PDF
    # downloads + Anthropic extraction. Same coalesce / max_instances
    # safety as the other jobs.
    if settings.enrichment_worker_enabled:
        _scheduler.add_job(
            enrichment_worker_job,
            trigger=IntervalTrigger(
                minutes=settings.enrichment_worker_interval_minutes
            ),
            id="enrichment_worker",
            next_run_time=None,
            coalesce=True,
            max_instances=1,
        )
        logger.info(
            "scheduler.enrichment_worker_scheduled",
            interval_minutes=settings.enrichment_worker_interval_minutes,
            batch_size=settings.enrichment_worker_batch_size,
        )
    _scheduler.start()
    logger.info("scheduler.started", interval_minutes=settings.poll_interval_minutes)


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


async def trigger_now() -> None:
    """Run a poll cycle immediately. Useful for testing or API-driven triggers."""
    asyncio.create_task(poll_all())
