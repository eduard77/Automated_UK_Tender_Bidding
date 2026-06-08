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

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from tender_agent.api.deps import require_account
from tender_agent.db import get_db
from tender_agent.diagnostics.cf_survey import (
    DEFAULT_SOURCES,
    iter_classified,
    summarise,
)

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
