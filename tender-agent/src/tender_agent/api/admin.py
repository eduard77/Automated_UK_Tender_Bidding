"""Admin endpoints — operator-only utilities for manually triggering extraction
and other one-shot operations. Not user-facing.

Existing admin routes live in `api/filters.py` (`/admin/poll-now`) for legacy
reasons; new admin endpoints land here.
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from tender_agent.config import settings
from tender_agent.db import get_db
from tender_agent.models import Tender
from tender_agent.schemas import TenderRequirementsRead
from tender_agent.services.discovery.proactis_discovery import (
    DiscoveryRunResult,
)
from tender_agent.services.discovery.proactis_discovery import (
    run_blocking as run_proactis_discovery_blocking,
)
from tender_agent.services.discovery.proactis_filter_config import (
    ProactisFilterConfig,
)
from tender_agent.services.requirements_extractor import extract_requirements

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post(
    "/extract-requirements/{tender_id}",
    response_model=TenderRequirementsRead,
)
def trigger_extract_requirements(
    tender_id: int, db: Session = Depends(get_db)
) -> TenderRequirementsRead:
    """Re-run requirements extraction for a single tender, overwriting any
    existing TenderRequirements row.

    Used by:
    - the dashboard's "Generate brief" button on the tender detail page
    - the `scripts/validate_extractor.py` validation harness
    - operators investigating a specific tender by hand

    Status codes:
    - 200: extraction succeeded, returns the new TenderRequirementsRead.
    - 404: tender does not exist.
    - 422: tender has no description AND no downloaded documents — there's
      nothing to extract from. Run the document downloader first.
    - 503: ANTHROPIC_API_KEY is not configured on the backend.
    - 502: the extractor was called but failed (Claude error, JSON parse error,
      etc.). Check structured logs for `requirements.api_error` /
      `requirements.parse_failed`.
    """
    tender = db.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(status_code=404, detail="tender not found")

    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=503,
            detail="anthropic_api_key not configured on the backend",
        )

    has_description = bool(tender.description and tender.description.strip())
    has_documents = bool(tender.document_files)
    if not has_description and not has_documents:
        raise HTTPException(
            status_code=422,
            detail=(
                "tender has no description and no downloaded documents — "
                "nothing to extract from"
            ),
        )

    record = extract_requirements(db, tender)
    if record is None:
        # extract_requirements returns None on Anthropic API error or JSON parse
        # failure. The structured log line is the source of truth; the operator
        # gets a generic 502 here.
        raise HTTPException(
            status_code=502,
            detail="extractor failed — check logs for requirements.api_error / parse_failed",
        )
    return record


# ---------------------------------------------------------------------------
# Proactis discovery (chunk 9)
# ---------------------------------------------------------------------------


class ProactisDiscoveryTriggerResponse(BaseModel):
    """Response body for the manual discovery trigger. The endpoint kicks the
    run off in the background, so this is just an ack — the actual outcome is
    written to the `poll_runs` row referenced by `poll_run_id_hint` (or
    inspectable via structured `discovery.proactis.*` log events)."""

    status: str  # "scheduled"
    detail: str


@router.post(
    "/discovery/proactis/run",
    status_code=202,
    response_model=ProactisDiscoveryTriggerResponse,
)
def trigger_proactis_discovery(
    background_tasks: BackgroundTasks,
) -> ProactisDiscoveryTriggerResponse:
    """Manually trigger a Proactis (procontract.due-north.com) discovery run.

    Reads the PUBLIC "Find Opportunities" listing over plain HTTP — no login, no
    bridge, no browser. Background-only — the run happens in a worker thread so
    the HTTP response returns immediately. Outcome (pages walked, rows seen,
    inserted / updated / deduped) is written to the latest `poll_runs` row for
    source PROACTIS and to the `discovery.proactis.*` structured logs.

    Filter config comes from `settings.proactis_discovery_*`.
    """
    config = ProactisFilterConfig(
        keywords=settings.proactis_discovery_keywords,
        regions=list(settings.proactis_discovery_regions),
        categories=list(settings.proactis_discovery_categories),
        portals=list(settings.proactis_discovery_portals),
        organisations=list(settings.proactis_discovery_organisations),
        include_closed=settings.proactis_discovery_include_closed,
        max_pages=settings.proactis_discovery_max_pages,
    )
    background_tasks.add_task(_run_proactis_discovery_safely, config)
    return ProactisDiscoveryTriggerResponse(
        status="scheduled",
        detail="Proactis discovery started in the background. Check the "
        "`discovery.proactis.*` structured logs (or the latest `poll_runs` "
        "row for source PROACTIS) for the outcome.",
    )


def _run_proactis_discovery_safely(config: ProactisFilterConfig) -> DiscoveryRunResult:
    """Background-task wrapper. Catches every exception so a discovery
    failure never propagates back to FastAPI's background-task runner (which
    would just log it under a generic name); the discovery service already
    writes structured `discovery.proactis.failed` events with full context.
    """
    try:
        return run_proactis_discovery_blocking(config)
    except Exception:  # noqa: BLE001
        # Already logged inside `run()`; the wrapper guarantees no exception
        # escapes BackgroundTasks. Return a synthetic "error" result so any
        # in-process caller (tests) gets a typed object back.
        return DiscoveryRunResult(status="error", error="exception in background task")
