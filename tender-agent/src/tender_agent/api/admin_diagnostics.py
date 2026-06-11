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

from tender_agent.api.deps import require_account
from tender_agent.config import settings
from tender_agent.db import get_db
from tender_agent.diagnostics.cf_survey import (
    DEFAULT_SOURCES,
    iter_classified,
    summarise,
)
from tender_agent.models import PollRun, Source, Tender

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


class SourceHealthRead(BaseModel):
    """Everything needed to diagnose ONE source's silence at a glance.

    `diagnosis` is a coarse machine-made hint mapping the observed state
    onto the harness's failure classes: "never_polled" (scheduler /
    registration), "fetch_failing" (upstream errors — the error string
    says whether it's a 403 datacenter block, a 500, or DNS), "polling_
    but_zero_rows" (fetch fine, nothing yielded — filter/format problem),
    or "healthy"."""

    code: str
    name: str
    enabled: bool
    last_polled_at: str | None
    tender_count: int
    latest_runs: list[PollRunRead]
    diagnosis: str


class SourcesHealthResponse(BaseModel):
    generated_at: str
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


def _diagnose(
    tender_count: int,
    runs: list[PollRun],
    enabled: bool,
    *,
    now: datetime,
) -> str:
    """Map per-source observed state onto the harness failure classes.

    Order matters here — every branch is decided on `runs[0]`, the
    newest run, NOT a soft "did ANY run error" predicate. The previous
    `any(r.error for r in runs)` rule tripped fetch_failing for sources
    whose newest run was clean (the 2026-06-11 12:52Z readout showed
    PROACTIS reading fetch_failing despite its newest run being status
    "ok" — an older orphaned-then-errored run was the trip).

    `stale_not_polling` is new: a "healthy" source whose newest run is
    multiple poll intervals old means the scheduler itself isn't
    firing, not that the source is healthy. The CF/FTS/PCS case in the
    same readout had nine-day-old runs reading "healthy".
    """
    if not enabled:
        return "disabled"
    if not runs:
        return "never_polled"
    newest = runs[0]
    if newest.status == "error":
        return "fetch_failing"
    finished_at = _coerce_aware(newest.finished_at)
    if finished_at is not None:
        stale_after = timedelta(
            minutes=settings.poll_interval_minutes * STALE_POLL_FACTOR
        )
        if now - finished_at > stale_after:
            return "stale_not_polling"
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
                    )
                    for r in runs
                ],
                diagnosis=_diagnose(
                    tender_count,
                    list(runs),
                    bool(source.enabled),
                    now=now,
                ),
            )
        )
    return SourcesHealthResponse(
        generated_at=now.isoformat(), sources=out
    )
