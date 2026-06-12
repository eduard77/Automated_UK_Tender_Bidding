"""Admin endpoints — operator-only utilities for manually triggering extraction
and other one-shot operations. Not user-facing.

Existing admin routes live in `api/filters.py` (`/admin/poll-now`) for legacy
reasons; new admin endpoints land here.
"""
from __future__ import annotations

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from tender_agent.api.credentials import DEFAULT_USER as CREDENTIALS_DEFAULT_USER
from tender_agent.config import settings
from tender_agent.db import get_db
from tender_agent.models import FilterProfile, Tender
from tender_agent.schemas import TenderRequirementsRead
from tender_agent.services.credentials import (
    CredentialsStoreError,
    get_store,
)
from tender_agent.services.discovery.proactis_discovery import (
    DiscoveryRunResult,
    run_for_profile_blocking,
)
from tender_agent.services.discovery.proactis_discovery import (
    run_blocking as run_proactis_discovery_blocking,
)
from tender_agent.services.discovery.proactis_filter_config import (
    ProactisFilterConfig,
)
from tender_agent.services.requirements_extractor import extract_requirements

router = APIRouter(prefix="/admin", tags=["admin"])
logger = structlog.get_logger(__name__)


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
# Sector classification backfill (Phase 3a)
# ---------------------------------------------------------------------------


class ClassificationBackfillResponse(BaseModel):
    """Ack for the one-time classification backfill. Fire-and-forget: the
    Batch-API submission + collection happens in the background; the operator
    watches `classification.backfill_*` logs and the
    GET /admin/diagnostics/classification-coverage readout for the outcome."""

    status: str  # "scheduled"
    detail: str
    limit: int


@router.post(
    "/classify/backfill",
    status_code=202,
    response_model=ClassificationBackfillResponse,
)
def trigger_classification_backfill(
    background_tasks: BackgroundTasks,
    limit: int | None = Query(
        default=None,
        ge=1,
        description="Max tenders to submit this batch (default: "
        "classifier_backfill_batch_size). Re-run to drain more — resumable "
        "via the classifier_version watermark.",
    ),
) -> ClassificationBackfillResponse:
    """Trigger a one-time classification backfill over the unclassified pool.

    Submits up to `limit` not-yet-classified tenders through the Anthropic
    Batch API (≈50% cheaper), polls the batch to completion in the background,
    and writes each sector back onto its tender. Bounded + resumable +
    idempotent: re-running picks up only tenders still off the current
    classifier version, so it's safe to call repeatedly to drain a large
    backlog.

    503 if `ANTHROPIC_API_KEY` is not configured.
    """
    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=503,
            detail="anthropic_api_key not configured on the backend",
        )
    effective_limit = limit or settings.classifier_backfill_batch_size
    background_tasks.add_task(_run_classification_backfill_safely, effective_limit)
    return ClassificationBackfillResponse(
        status="scheduled",
        detail=(
            f"Classification backfill of up to {effective_limit} tenders started "
            "in the background (Batch API). Watch `classification.backfill_*` "
            "logs and GET /admin/diagnostics/classification-coverage. Re-run to "
            "drain more — it's resumable."
        ),
        limit=effective_limit,
    )


def _run_classification_backfill_safely(limit: int) -> None:
    """Background wrapper — never lets an exception escape into FastAPI's
    background-task runner; the backfill already logs structured events."""
    try:
        from tender_agent.services.classification.backfill import run_backfill

        run_backfill(limit=limit)
    except Exception:  # noqa: BLE001
        logger.exception("classification.backfill_failed")


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

    Background-only — the run happens in a worker thread so the HTTP response
    returns immediately (browser-driven discovery takes minutes, not seconds).
    The bridge must be reachable AND the operator must be logged in at the
    bridge window; the run logs `discovery.proactis.needs_login` if not and
    finishes cleanly without raising.

    Filter config comes from `settings.proactis_discovery_*` (Step 1). Step 2
    will wire the dashboard's main filter page to this same config object.
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


# ---------------------------------------------------------------------------
# Logged-in, profile-driven Proactis discovery (Step 1)
# ---------------------------------------------------------------------------


class ProactisProfileRunTriggerRequest(BaseModel):
    """Body for the logged-in profile-driven discovery trigger.

    Step 1 scope: single operator, single filter profile, single portal/user
    record in the credentials store. We accept the IDs from the body rather
    than infer them so the operator can pick a specific profile + credential
    pair if they have several saved.
    """

    profile_id: int
    portal_id: int
    # Default matches the user POST /credentials stores under
    # (api.credentials.DEFAULT_USER) — so "store the login, then trigger the
    # run" works without the caller having to know about user ids at all.
    user_id: str = CREDENTIALS_DEFAULT_USER


class ProactisProfileRunTriggerResponse(BaseModel):
    status: str  # "scheduled"
    detail: str


@router.post(
    "/discovery/proactis/run-for-profile",
    status_code=202,
    response_model=ProactisProfileRunTriggerResponse,
)
def trigger_proactis_profile_discovery(
    body: ProactisProfileRunTriggerRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> ProactisProfileRunTriggerResponse:
    """Manually trigger a LOGGED-IN, profile-filtered Proactis discovery run
    against the operator's existing FilterProfile.

    Pulls the stored credentials from the same encrypted CredentialsStore
    Delta uses; never logs the password or surfaces it in the response.
    The run drives Proactis as a search user (NOT a registrant) and goes
    through the same parse / upsert / dedup path CF and FTS use, so
    discovered tenders land in the unified search and dedup on the DN
    reference.

    The trigger is fire-and-forget: discovery takes minutes (multiple
    pages + per-row detail reads), so we return 202 + a hint to read the
    structured `discovery.proactis.profile_*` logs or the latest `poll_runs`
    row for source PROACTIS.
    """
    profile = db.get(FilterProfile, body.profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="filter_profile_not_found")

    try:
        store = get_store()
        credentials = store.get_credentials(body.portal_id, body.user_id)
    except CredentialsStoreError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"credentials_store_unavailable: {exc}",
        ) from exc
    if credentials is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "no_proactis_credentials_stored — POST credentials to "
                "/credentials first for this portal_id + user_id"
            ),
        )

    # Detach the profile from the session so the background thread can use
    # it without keeping the request's DB session open. Snapshot the fields
    # the discovery actually reads.
    snapshot = _profile_snapshot(profile)
    background_tasks.add_task(
        _run_proactis_profile_discovery_safely, credentials, snapshot
    )
    return ProactisProfileRunTriggerResponse(
        status="scheduled",
        detail=(
            "Profile-driven Proactis discovery scheduled. Check the "
            "`discovery.proactis.profile_*` structured logs or the latest "
            "`poll_runs` row for source PROACTIS for the outcome."
        ),
    )


class _ProfileSnapshot:
    """Tiny attribute carrier the discovery layer reads via getattr().

    A FilterProfile from the request's DB session can't be safely accessed
    inside the BackgroundTasks worker (session is closed); copy only the
    fields the run actually uses."""

    __slots__ = ("id", "cpv_codes", "cpv_prefixes", "regions", "keywords_any")

    def __init__(self, profile: FilterProfile) -> None:
        self.id = profile.id
        self.cpv_codes = list(profile.cpv_codes or [])
        self.cpv_prefixes = list(profile.cpv_prefixes or [])
        self.regions = list(profile.regions or [])
        self.keywords_any = list(profile.keywords_any or [])


def _profile_snapshot(profile: FilterProfile) -> _ProfileSnapshot:
    return _ProfileSnapshot(profile)


def _run_proactis_profile_discovery_safely(
    credentials, profile: _ProfileSnapshot
) -> DiscoveryRunResult:
    """Background wrapper — never lets an exception escape into the FastAPI
    background-task runner's generic logging."""
    try:
        # Reuse SessionLocal as the db_factory so the run owns its own DB
        # session for the lifetime of the cycle (mirrors `run()`).
        from tender_agent.services.discovery.proactis_discovery import (
            run_for_profile_blocking as _blocking,
        )

        # _blocking does `asyncio.run(run_for_profile(...))` which uses
        # SessionLocal as db_factory by default — perfect.
        return _blocking(credentials=credentials, profile=profile)
    except Exception:  # noqa: BLE001
        return DiscoveryRunResult(
            status="error", error="exception in background task"
        )


# Re-exported for tests that want to drive the background work directly.
__all__ = [
    "_ProfileSnapshot",
    "_run_proactis_discovery_safely",
    "_run_proactis_profile_discovery_safely",
    "router",
    "run_for_profile_blocking",
]
