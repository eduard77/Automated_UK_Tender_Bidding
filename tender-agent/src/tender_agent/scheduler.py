"""Polling scheduler. Drives source ingestion at the configured interval."""
from __future__ import annotations

import asyncio

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from tender_agent.adapters import ADAPTERS
from tender_agent.config import settings
from tender_agent.db import SessionLocal
from tender_agent.models import Source
from tender_agent.services.discovery.proactis_discovery import (
    run as run_proactis_discovery,
)
from tender_agent.services.discovery.proactis_filter_config import (
    ProactisFilterConfig,
)
from tender_agent.services.email.poller import poll_all_mailboxes
from tender_agent.services.ingestion import poll_source

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


async def poll_all() -> None:
    """Poll every enabled source sequentially."""
    with SessionLocal() as db:
        sources = db.execute(select(Source).where(Source.enabled.is_(True))).scalars().all()
        for source in sources:
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
