"""Operator-only diagnostic endpoints, runnable from the FastAPI /docs page.

    GET /admin/diagnostics/cf-onward-routes  — CF/FTS onward-route survey (JSON)

This is a thin, READ-ONLY wrapper around the survey shipped as a CLI in
``scripts/survey_cf_onward_routes.py``: it reuses the exact same scan + classify
logic (``tender_agent.diagnostics.cf_survey``) so the operator can run the survey
by clicking a button on /docs against the live database, without needing a shell
in the container. No writes, no schema changes, no network calls.

Gated behind ``require_account`` at the router level, the same way the Delta
session-test admin endpoints are — an anonymous caller gets 401.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tender_agent.adapters import ADAPTERS
from tender_agent.api.deps import require_account
from tender_agent.config import settings
from tender_agent.db import get_db
from tender_agent.diagnostics.cf_survey import (
    DEFAULT_SOURCES,
    iter_classified,
    summarise,
)
from tender_agent.models import PollRun, Source, Tender
from tender_agent.services.classification.taxonomy import CLASSIFIER_VERSION

router = APIRouter(
    prefix="/admin/diagnostics",
    tags=["admin"],
    dependencies=[Depends(require_account)],
)

#: How many per-tender rows to echo back for eyeballing (the full set lives in
#: the CLI's CSV; the API only returns a small sample).
SAMPLE_SIZE = 20


class BucketCounts(BaseModel):
    """Count of tenders in each of the four onward-route buckets."""

    direct_portal_link: int
    portal_generic_link: int
    email_only: int
    none: int


class PortalCount(BaseModel):
    portal: str
    count: int


class SampleRow(BaseModel):
    tender_id: int
    title: str | None
    bucket: str
    portal: str | None
    detail: str | None


class CfOnwardRouteSurveyResponse(BaseModel):
    total_scanned: int
    sources: list[str]
    buckets: BucketCounts
    direct_portal_breakdown: list[PortalCount]
    sample: list[SampleRow] = Field(
        default_factory=list,
        description=f"First {SAMPLE_SIZE} scanned tenders, for spot-checking.",
    )


@router.get("/cf-onward-routes", response_model=CfOnwardRouteSurveyResponse)
def cf_onward_routes_survey(
    limit: int | None = Query(
        default=None,
        ge=1,
        description="Cap the scan to the first N tenders (by id) for a quick spot-check.",
    ),
    sources: str = Query(
        default=",".join(DEFAULT_SOURCES),
        description="Comma-separated source codes to scan (default: CF,FTS).",
    ),
    db: Session = Depends(get_db),
) -> CfOnwardRouteSurveyResponse:
    """Survey how CF/FTS notices in the DB route suppliers onward to portals.

    Read-only. Returns the bucket counts, the per-portal breakdown of
    direct_portal_link (sorted by count desc — the signal for ranking which
    portal adapters to build next), and a small per-tender sample.
    """
    source_codes = tuple(s.strip().upper() for s in sources.split(",") if s.strip())
    results = list(iter_classified(db, source_codes, limit))
    summary = summarise(results)

    sample = [
        SampleRow(
            tender_id=tender.id,
            title=tender.title,
            bucket=cls.bucket,
            portal=cls.portal,
            detail=cls.detail,
        )
        for tender, cls in results[:SAMPLE_SIZE]
    ]

    return CfOnwardRouteSurveyResponse(
        total_scanned=summary.total_scanned,
        sources=list(source_codes),
        buckets=BucketCounts(**summary.buckets),
        direct_portal_breakdown=[
            PortalCount(portal=portal, count=count)
            for portal, count in summary.direct_portal_breakdown
        ],
        sample=sample,
    )


# ---------------------------------------------------------------------------
# Source-ingestion health — the "Log stream in one URL" diagnostic
# (Phase 1 of the build harness: why are Atamis / EU-Supply / Sell2Wales
# silent? The answer lives in each source's latest PollRuns, which only the
# Azure Log stream used to show.)
# ---------------------------------------------------------------------------

#: Most recent poll runs echoed per source. Three is enough to tell
#: "always erroring" from "flapping" from "never ran".
POLL_RUNS_PER_SOURCE = 3


class PollRunRead(BaseModel):
    """One poll attempt, newest first."""

    started_at: str
    finished_at: str | None
    status: str  # "running" | "ok" | "error"
    fetched: int
    new_count: int
    updated_count: int
    error: str | None
    # Resume point this run reached (the confirmed-page watermark). Lets the
    # operator SEE a backlog drain climbing toward present across cycles.
    watermark_at: str | None = None


class SourceHealthRead(BaseModel):
    """Everything needed to diagnose ONE source's silence at a glance.

    `diagnosis` is a coarse machine-made hint mapping the observed state
    onto the harness's failure classes: "never_polled" (scheduler /
    registration), "fetch_failing" (upstream errors — the error string
    says whether it's a 403 datacenter block, a 500, or DNS), "catching_up"
    (newest run errored/timed-out but its resume watermark ADVANCED — a long
    backlog drain converging, NOT a fault), "polling_
    but_zero_rows" (fetch fine, nothing yielded — filter/format problem),
    "stale_not_polling" (an HTTP source whose newest clean run is many poll
    intervals old — the scheduler isn't firing), "needs_login" (a
    browser-driven source whose newest run ended waiting for a human login),
    "manual_browser_discovery" (a browser-driven source — PROACTIS — that
    runs on its OWN login-gated, operator/scheduled browser cycle, NOT the
    HTTP poll loop; an older last-run is expected, NOT a dead scheduler), or
    "healthy"."""

    code: str
    name: str
    enabled: bool
    last_polled_at: str | None
    tender_count: int
    latest_runs: list[PollRunRead]
    diagnosis: str


class SchedulerHeartbeatRead(BaseModel):
    """Liveness of the poll scheduler itself (2026-06-12 outage: per-source
    diagnoses said `stale_not_polling` while the actual fault was a DEAD
    scheduler — this block makes that distinction readable at a glance)."""

    scheduler_running: bool
    process_started_at: str | None
    last_cycle_started_at: str | None
    last_cycle_finished_at: str | None
    cycle_running: bool
    last_cycle_error: str | None
    next_cycle_at: str | None


class SourcesHealthResponse(BaseModel):
    generated_at: str
    scheduler: SchedulerHeartbeatRead
    sources: list[SourceHealthRead]


#: If the newest poll run finished more than this many poll-intervals ago
#: the source is `stale_not_polling`. The 2026-06-11 12:52Z readout showed
#: CF/FTS/PCS reading "healthy" with newest-run timestamps from 2026-06-02
#: — nine days stale, the scheduler had not actually fired since. Four
#: intervals is conservative (a 30-min poll = a 2-hour grace) so a single
#: missed cycle never trips the flag.
STALE_POLL_FACTOR = 4


def _coerce_aware(value: datetime | None) -> datetime | None:
    """SQLite drops tzinfo on round-trip; treat naive datetimes as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _is_catching_up(runs: list[PollRun]) -> bool:
    """True when the newest (errored/timed-out) run is still making FORWARD
    progress through a backlog — its resume watermark advanced past the
    previous run's.

    This is the signal that a 900s-timeout on CF/FTS is a long backlog DRAIN,
    not a fault: each 15-min cycle cancels mid-sweep but keeps + advances its
    resume point (poll_source persists `watermark_at` per confirmed page), so
    successive runs climb toward present and eventually complete `ok`. A
    genuinely stuck/failing source confirms no page (watermark_at NULL) or
    never advances it (same value run-to-run) — that stays `fetch_failing`."""
    newest_wm = _coerce_aware(getattr(runs[0], "watermark_at", None))
    if newest_wm is None:
        return False
    prev_wm = (
        _coerce_aware(getattr(runs[1], "watermark_at", None))
        if len(runs) > 1
        else None
    )
    # First run that made progress, or a strictly-advanced resume point.
    return prev_wm is None or newest_wm > prev_wm


def _diagnose(
    tender_count: int,
    runs: list[PollRun],
    enabled: bool,
    *,
    now: datetime,
    http_polled: bool = True,
) -> str:
    """Map per-source observed state onto the harness failure classes.

    Order matters here — every branch is decided on `runs[0]`, the
    newest run, NOT a soft "did ANY run error" predicate. The previous
    `any(r.error for r in runs)` rule tripped fetch_failing for sources
    whose newest run was clean (the 2026-06-11 12:52Z readout showed
    PROACTIS reading fetch_failing despite its newest run being status
    "ok" — an older orphaned-then-errored run was the trip).

    `stale_not_polling` is new (2026-06-11 rev 3): a "healthy" source whose
    newest run is multiple poll intervals old means the scheduler itself
    isn't firing, not that the source is healthy. The CF/FTS/PCS case in the
    same readout had nine-day-old runs reading "healthy".

    `http_polled` distinguishes the HTTP poll-loop sources (the ADAPTERS
    registry) from browser-driven ones (PROACTIS — login-gated, run on its
    own browser cycle via scheduler.proactis_discovery_job / run_for_profile,
    deliberately NOT part of the HTTP poll loop; see _poll_one_source which
    skips it). The HTTP staleness rule is a category error for a browser
    source: an older last-run is EXPECTED, not a dead scheduler. So a stale
    browser source reads `manual_browser_discovery` (a note, not a fault),
    and a browser run left waiting for a human login reads `needs_login`
    (2026-06-14: PROACTIS showed `stale_not_polling` — a false alarm — while
    every HTTP source polled normally).
    """
    if not enabled:
        return "disabled"
    if not runs:
        return "never_polled"
    newest = runs[0]
    if newest.status == "error":
        # A timed-out catch-up run that ADVANCED its resume point is draining
        # a backlog, not failing — don't read it as a fault (2026-06-14).
        return "catching_up" if _is_catching_up(runs) else "fetch_failing"
    # Browser-driven discovery (run_for_profile / proactis_discovery_job)
    # finalises a run as "needs_login" when the bridge isn't authenticated;
    # surface that as the actionable state rather than a generic class.
    if newest.status == "needs_login":
        return "needs_login"
    finished_at = _coerce_aware(newest.finished_at)
    if finished_at is not None:
        stale_after = timedelta(
            minutes=settings.poll_interval_minutes * STALE_POLL_FACTOR
        )
        if now - finished_at > stale_after:
            return "stale_not_polling" if http_polled else "manual_browser_discovery"
    if tender_count == 0:
        return "polling_but_zero_rows"
    return "healthy"


@router.get("/sources-health", response_model=SourcesHealthResponse)
def sources_health(db: Session = Depends(get_db)) -> SourcesHealthResponse:
    """Per-source ingestion health: enabled flag, watermark, tender count,
    the latest poll runs (status + error strings), and a coarse diagnosis.

    READ-ONLY. This is the browser-accessible version of "open the Log
    stream and grep for atamis/eu_supply/sell2wales" — the operator clicks
    once on /docs and pastes the JSON back. The `error` strings are the
    adapters' own exception texts (an upstream 403 vs 500 vs DNS failure
    reads directly off them)."""
    sources = (
        db.execute(select(Source).order_by(Source.code)).scalars().all()
    )
    out: list[SourceHealthRead] = []
    now = datetime.now(UTC)
    for source in sources:
        runs = (
            db.execute(
                select(PollRun)
                .where(PollRun.source_id == source.id)
                .order_by(PollRun.started_at.desc())
                .limit(POLL_RUNS_PER_SOURCE)
            )
            .scalars()
            .all()
        )
        tender_count = int(
            db.execute(
                select(func.count(Tender.id)).where(
                    Tender.source_code == source.code
                )
            ).scalar_one()
        )
        out.append(
            SourceHealthRead(
                code=source.code,
                name=source.name,
                enabled=bool(source.enabled),
                last_polled_at=(
                    source.last_polled_at.isoformat()
                    if source.last_polled_at
                    else None
                ),
                tender_count=tender_count,
                latest_runs=[
                    PollRunRead(
                        started_at=r.started_at.isoformat(),
                        finished_at=(
                            r.finished_at.isoformat() if r.finished_at else None
                        ),
                        status=r.status,
                        fetched=r.fetched,
                        new_count=r.new_count,
                        updated_count=r.updated_count,
                        error=r.error,
                        watermark_at=(
                            r.watermark_at.isoformat() if r.watermark_at else None
                        ),
                    )
                    for r in runs
                ],
                diagnosis=_diagnose(
                    tender_count,
                    list(runs),
                    bool(source.enabled),
                    now=now,
                    # Browser-driven sources (PROACTIS) aren't in ADAPTERS;
                    # the HTTP staleness rule doesn't apply to them.
                    http_polled=source.code in ADAPTERS,
                ),
            )
        )
    return SourcesHealthResponse(
        generated_at=now.isoformat(),
        scheduler=_scheduler_heartbeat_read(),
        sources=out,
    )


def _iso_or_none(value) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _scheduler_heartbeat_read() -> SchedulerHeartbeatRead:
    from tender_agent import scheduler as scheduler_module

    beat = scheduler_module.heartbeat()
    return SchedulerHeartbeatRead(
        scheduler_running=bool(beat.get("scheduler_running")),
        process_started_at=_iso_or_none(beat.get("process_started_at")),
        last_cycle_started_at=_iso_or_none(beat.get("last_cycle_started_at")),
        last_cycle_finished_at=_iso_or_none(beat.get("last_cycle_finished_at")),
        cycle_running=bool(beat.get("cycle_running")),
        last_cycle_error=beat.get("last_cycle_error"),
        next_cycle_at=_iso_or_none(beat.get("next_cycle_at")),
    )


# ---------------------------------------------------------------------------
# Sector classification coverage — the Phase-3a gate readout
# ---------------------------------------------------------------------------


class SectorCount(BaseModel):
    sector: str | None  # None = not yet classified
    count: int


class SourceClassificationCount(BaseModel):
    source_code: str
    total: int
    classified: int


class ClassificationCoverageResponse(BaseModel):
    """How far sector classification has populated, across ALL sources.

    The Phase-3a gate: paste this back to prove classification landed — a
    Construction count, an Other count, multiple sectors present, and the
    formerly-silent sources (PROACTIS / ATAMIS / EU_SUPPLY) now carrying a
    sector in `by_source.classified`."""

    classifier_version: str
    total_tenders: int
    classified: int
    unclassified: int
    by_primary_sector: list[SectorCount]
    by_source: list[SourceClassificationCount]


@router.get(
    "/classification-coverage", response_model=ClassificationCoverageResponse
)
def classification_coverage(
    db: Session = Depends(get_db),
) -> ClassificationCoverageResponse:
    """Count of tenders by primary_sector + per-source classified coverage.

    READ-ONLY. Evidence for the Phase-3a gate that AI sector classification
    populated across every source."""
    total = int(db.execute(select(func.count(Tender.id))).scalar_one())
    classified = int(
        db.execute(
            select(func.count(Tender.id)).where(Tender.primary_sector.isnot(None))
        ).scalar_one()
    )

    sector_rows = db.execute(
        select(Tender.primary_sector, func.count(Tender.id))
        .group_by(Tender.primary_sector)
        .order_by(func.count(Tender.id).desc())
    ).all()
    by_primary_sector = [
        SectorCount(sector=row[0], count=int(row[1])) for row in sector_rows
    ]

    source_rows = db.execute(
        select(
            Tender.source_code,
            func.count(Tender.id),
            func.count(Tender.primary_sector),
        )
        .group_by(Tender.source_code)
        .order_by(Tender.source_code)
    ).all()
    by_source = [
        SourceClassificationCount(
            source_code=row[0], total=int(row[1]), classified=int(row[2])
        )
        for row in source_rows
    ]

    return ClassificationCoverageResponse(
        classifier_version=CLASSIFIER_VERSION,
        total_tenders=total,
        classified=classified,
        unclassified=total - classified,
        by_primary_sector=by_primary_sector,
        by_source=by_source,
    )
