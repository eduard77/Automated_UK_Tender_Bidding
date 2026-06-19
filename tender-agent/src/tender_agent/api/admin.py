"""Admin endpoints — operator-only utilities for manually triggering extraction
and other one-shot operations. Not user-facing.

Existing admin routes live in `api/filters.py` (`/admin/poll-now`) for legacy
reasons; new admin endpoints land here.
"""
from __future__ import annotations

import contextlib
import threading

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.api.credentials import DEFAULT_USER as CREDENTIALS_DEFAULT_USER
from tender_agent.config import settings
from tender_agent.db import get_db
from tender_agent.models import FilterProfile, Tender, TenderRequirements
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
from tender_agent.services.document_downloader import download_documents_for_tender
from tender_agent.services.requirements_extractor import extract_requirements

router = APIRouter(prefix="/admin", tags=["admin"])
logger = structlog.get_logger(__name__)


@router.post(
    "/extract-requirements/{tender_id}",
    response_model=TenderRequirementsRead,
)
async def trigger_extract_requirements(
    tender_id: int,
    db: Session = Depends(get_db),
    force: bool = Query(
        False,
        description="Re-run extraction even if requirements are already stored "
        "(overwrites + re-charges). Default false: an already-extracted tender "
        "returns its stored requirements untouched.",
    ),
) -> TenderRequirementsRead:
    """Extract a tender's requirements ON-DEMAND — the human-interest trigger
    that replaced the always-on enrichment worker (cost decision 2026-06-19,
    docs/document-fetch-cost-audit.md). The dashboard tender-detail page calls
    this the first time a human opens a tender.

    Idempotent by default: if a TenderRequirements row already exists this
    returns it as-is — no document download, no Anthropic call, no re-charge.
    Pass `force=true` to overwrite (operators re-investigating, and the
    `scripts/validate_extractor.py` harness call extract_requirements directly,
    not this endpoint).

    On a first extraction it downloads the tender's documents first (reusing the
    unchanged document downloader) so the Sonnet call analyses the actual ITT
    text rather than the description alone — the worker used to do this download
    on the same tenders; only the trigger moved to on-demand.

    Status codes:
    - 200: returns the TenderRequirementsRead (freshly extracted, or the stored
      row when one already existed and force is false).
    - 404: tender does not exist.
    - 422: tender has no description AND no extractable documents — nothing to
      extract from.
    - 503: ANTHROPIC_API_KEY is not configured on the backend.
    - 502: the extractor was called but failed (Claude error, JSON parse error,
      etc.). Check structured logs for `requirements.api_error` /
      `requirements.parse_failed`.
    """
    tender = db.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(status_code=404, detail="tender not found")

    # Idempotency / caching: opening an already-analysed tender must never
    # re-run extraction or re-charge. Checked before the API-key gate so a
    # stored brief is still readable even if the key is later unset.
    if not force:
        existing = db.execute(
            select(TenderRequirements).where(
                TenderRequirements.tender_id == tender_id
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=503,
            detail="anthropic_api_key not configured on the backend",
        )

    # Fetch the documents first so extraction analyses the ITT pack, not just
    # the description. Best-effort: a flaky download must not block extraction —
    # extract_requirements falls back to the description when no document text
    # is available. Reuses the existing downloader unchanged (the fetch
    # mechanism is not altered; only when it runs for this tender).
    try:
        await download_documents_for_tender(db, tender)
        db.refresh(tender)
    except Exception:  # noqa: BLE001
        logger.exception("extract.document_download_failed", tender_id=tender_id)

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
    GET /admin/diagnostics/classification-coverage readout for the outcome.
    Returns 409 while a previous backfill run is still in progress."""

    status: str  # "scheduled"
    detail: str
    limit: int


#: Only one backfill run at a time. A chunked 10k run can take hours, so an
#: accidental second trigger would otherwise select overlapping pending rows
#: and double-buy them (collect's skip-current prevents data corruption but
#: not double payment) and put two of our batches in flight at once.
#: Process-local is sufficient: the container runs a single uvicorn worker.
_backfill_lock = threading.Lock()


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
    Batch API (≈50% cheaper) in sequential chunks, polls each chunk to
    completion in the background, and writes each sector back onto its
    tender. Bounded + resumable + idempotent: re-running picks up only
    tenders still off the current classifier version, so it's safe to call
    repeatedly to drain a large backlog. On start it self-heals state a
    previously-failed run left behind (adopts a still-in-flight batch,
    collects ended-but-uncollected ones).

    503 if `ANTHROPIC_API_KEY` is not configured. 409 if a backfill run is
    already in progress (re-trigger after it completes).
    """
    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=503,
            detail="anthropic_api_key not configured on the backend",
        )
    # Acquired HERE (not in the background task) so two quick POSTs can't
    # both schedule before either run starts. Released in the task's finally.
    if not _backfill_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail="classification backfill already running — watch "
            "`classification.backfill_*` logs; re-trigger after it completes",
        )
    effective_limit = limit or settings.classifier_backfill_batch_size
    try:
        background_tasks.add_task(
            _run_classification_backfill_safely, effective_limit
        )
    except BaseException:
        _backfill_lock.release()
        raise
    return ClassificationBackfillResponse(
        status="scheduled",
        detail=(
            f"Classification backfill of up to {effective_limit} tenders started "
            "in the background (Batch API, chunked). Watch "
            "`classification.backfill_*` logs and "
            "GET /admin/diagnostics/classification-coverage. Re-run to drain "
            "more — it's resumable. A second trigger while one is running "
            "returns 409."
        ),
        limit=effective_limit,
    )


def _run_classification_backfill_safely(limit: int) -> None:
    """Background wrapper — never lets an exception escape into FastAPI's
    background-task runner. `run_backfill` guards every phase internally and
    logs structured `classification.backfill_*` events itself; this outer
    catch is belt-and-braces, and (2026-06-12 incident) it must carry the
    exception type/message as PLAIN fields — `exc_info` alone is not rendered
    by the Azure log stream, which left the first 10k failure undiagnosable."""
    try:
        from tender_agent.services.classification.backfill import run_backfill

        run_backfill(limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "classification.backfill_failed",
            phase="background_task",
            error_type=type(exc).__name__,
            error=str(exc)[:500],
            limit=limit,
        )
    finally:
        # Paired with the acquire in trigger_classification_backfill. The
        # suppress keeps direct test invocations (which never acquired) safe.
        with contextlib.suppress(RuntimeError):
            _backfill_lock.release()


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
