"""PortalOrchestrator — the 'click Fetch documents' flow up to documents on
disk.

Chunk 3 makes this real for Contracts Finder: CF tenders route to the
ContractsFinderDirectAdapter, which downloads public assets.publishing
documents; everything else falls back to a best-effort public download.
Fetched files are persisted as TenderDocumentFile rows (sha256-deduped, so the
flow is idempotent). Real authenticated portal adapters land in chunk 4+.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.config import settings
from tender_agent.models import (
    FetchTask,
    Portal,
    PortalUrlSighting,
    Tender,
    TenderDocumentFile,
)
from tender_agent.services import preflight, push
from tender_agent.services.bridge_client import BridgeClient
from tender_agent.services.credentials import CredentialsStore
from tender_agent.services.portals.base import (
    Credentials,
    PortalAdapter,
    PortalContext,
)
from tender_agent.services.portals.contracts_finder import (
    ContractsFinderDirectAdapter,
)
from tender_agent.services.portals.registry import (
    get_adapter_for_platform,
    get_fallback_adapter,
)
from tender_agent.services.portals.results import (
    AuthStatus,
    DownloadResult,
    DownloadStatus,
    LocateStatus,
    RegisterStatus,
)

logger = structlog.get_logger(__name__)

# Source codes whose tenders route to the Contracts Finder direct adapter.
CF_SOURCE_CODES = {"CF"}


class OrchestrationStatus(StrEnum):
    complete = "complete"
    partial = "partial"
    no_portal = "no_portal"
    needs_registration = "needs_registration"
    credentials_invalid = "credentials_invalid"
    needs_2fa = "needs_2fa"
    blocked = "blocked"
    locate_failed = "locate_failed"
    register_failed = "register_failed"
    download_failed = "download_failed"
    nothing_available = "nothing_available"
    # Login-via-human flow (chunk 4)
    waiting_for_login = "waiting_for_login"
    needs_user_confirmation = "needs_user_confirmation"
    bridge_unavailable = "bridge_unavailable"
    failed = "failed"
    error = "error"


@dataclass
class OrchestrationResult:
    status: OrchestrationStatus
    tender_id: int
    portal_id: int | None = None
    platform_slug: str | None = None
    adapter: str | None = None
    files: list[dict] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    files_persisted: int = 0
    detail: str | None = None


def _dest_dir(tender_id: int) -> str:
    """Per-run staging directory for adapters that download straight to a folder
    (the public-HTTP fallback / Contracts Finder path). Always rooted at the
    configured, absolute DOCUMENT_STORAGE_DIR — never a bare relative 'data'
    path, which fails the instant the working directory isn't writable (the
    PermissionError: 'data' seen in live testing — issue 4d-1)."""
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = os.environ.get("TENDER_AGENT_DOC_ROOT") or settings.document_storage_dir
    return str(Path(base) / str(tender_id) / ts)


def _candidate_urls(tender: Tender, sightings: list[PortalUrlSighting]) -> list[str]:
    """HTTP URLs worth trying for a direct download: the tender's documents[]
    plus link-type sightings on the chosen portal."""
    urls: list[str] = []
    for doc in tender.documents or []:
        if isinstance(doc, dict):
            u = doc.get("url")
            if isinstance(u, str) and u.startswith("http"):
                urls.append(u)
    for s in sightings:
        if s.sighting_type in ("tender_link", "document_link") and s.url.startswith(
            "http"
        ):
            urls.append(s.url)
    # de-dup, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


class PortalOrchestrator:
    def __init__(
        self,
        credentials_store: CredentialsStore | None = None,
        bridge: BridgeClient | None = None,
        login_wait_cycle_seconds: int = 600,
        login_wait_total_seconds: int = 3600,
        keepalive_enabled: bool = True,
        keepalive_interval_seconds: int = 150,
        keepalive_budget_seconds: int = 2400,
        pause_on_already_registered: bool = False,
    ) -> None:
        self._creds_store = credentials_store
        self._bridge = bridge
        # Wait-for-login cadence. Each cycle re-pokes the bridge; the total is
        # the budget before giving up (so the user can be away). Tests pass
        # tiny values.
        self.login_wait_cycle_seconds = login_wait_cycle_seconds
        self.login_wait_total_seconds = login_wait_total_seconds
        # Session keepalive while a fetch sits in the needs_user_confirmation
        # pause: a single Delta session times out after a few minutes of
        # inactivity, so we ping it periodically to keep it warm (chunk 4h).
        # Bounded — after the budget we let it expire; the resume re-auths.
        self.keepalive_enabled = keepalive_enabled
        self.keepalive_interval_seconds = keepalive_interval_seconds
        self.keepalive_budget_seconds = keepalive_budget_seconds
        self._keepalive_tasks: dict[str, asyncio.Task] = {}
        # When True the confirmation pause is kept even for already-registered
        # tenders. Default False: an already-registered tender has no intent-
        # signalling Register Interest click to gate, so we read its documents
        # without pausing (chunk 4h, Fix 7).
        self.pause_on_already_registered = pause_on_already_registered

    def _get_bridge(self) -> BridgeClient:
        return self._bridge if self._bridge is not None else BridgeClient()

    def _store(self) -> CredentialsStore | None:
        if self._creds_store is not None:
            return self._creds_store
        try:
            from tender_agent.services.credentials import get_store

            return get_store()
        except Exception as exc:  # noqa: BLE001
            logger.warning("orchestrator.no_credentials_store", error=str(exc))
            return None

    def _select_portal(
        self, db: Session, tender_id: int
    ) -> tuple[Portal | None, list[PortalUrlSighting]]:
        """The most relevant non-deprecated portal for this tender: highest
        priority, then most tenders, among portals with a link-type sighting
        on this tender."""
        sightings = list(
            db.execute(
                select(PortalUrlSighting).where(
                    PortalUrlSighting.tender_id == tender_id
                )
            ).scalars().all()
        )
        portal_ids = {
            s.portal_id
            for s in sightings
            if s.portal_id is not None
            and s.sighting_type in ("tender_link", "document_link")
        }
        if not portal_ids:
            return None, sightings
        priority_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        candidates = list(
            db.execute(
                select(Portal)
                .where(Portal.id.in_(portal_ids))
                .where(Portal.adapter_status != "deprecated")
            ).scalars().all()
        )
        if not candidates:
            return None, sightings
        candidates.sort(
            key=lambda p: (priority_rank.get(p.priority, 9), -(p.tender_count or 0))
        )
        chosen = candidates[0]
        chosen_sightings = [s for s in sightings if s.portal_id == chosen.id]
        return chosen, chosen_sightings

    async def fetch_tender_documents(
        self,
        tender_id: int,
        user_id: str = "eduard",
        db: Session | None = None,
        task_id: str | None = None,
        resume_from_confirm: bool = False,
    ) -> OrchestrationResult:
        if db is None:
            from tender_agent.db import SessionLocal

            with SessionLocal() as owned_db:
                return await self._run(
                    tender_id, user_id, owned_db, task_id, resume_from_confirm
                )
        return await self._run(tender_id, user_id, db, task_id, resume_from_confirm)

    def _select_adapter(
        self, tender: Tender, candidate_urls: list[str], portal: Portal | None
    ) -> tuple[PortalAdapter, str | None]:
        """Pick the adapter. A portal's registered platform adapter (Delta, CF,
        …) wins; then CF by source/asset-host URL; else the FallbackAdapter.

        Registered-first is what routes a Delta-hosted tender to the Delta
        adapter even when the tender was *listed* on Contracts Finder."""
        platform_slug = portal.platform.slug if (portal and portal.platform) else None
        registered = get_adapter_for_platform(platform_slug)
        if registered is not None:
            return registered(), platform_slug
        cf = ContractsFinderDirectAdapter()
        if tender.source_code in CF_SOURCE_CODES or any(
            cf.matches_url(u) for u in candidate_urls
        ):
            return cf, "contracts_finder_direct"
        return get_fallback_adapter()(), platform_slug

    async def _run(
        self,
        tender_id: int,
        user_id: str,
        db: Session,
        task_id: str | None = None,
        resume_from_confirm: bool = False,
    ) -> OrchestrationResult:
        tender = db.get(Tender, tender_id)
        if tender is None:
            return OrchestrationResult(
                status=OrchestrationStatus.error,
                tender_id=tender_id,
                detail="tender not found",
            )

        portal, sightings = self._select_portal(db, tender_id)
        candidate_urls = _candidate_urls(tender, sightings)

        adapter, platform_slug = self._select_adapter(tender, candidate_urls, portal)
        adapter_name = type(adapter).__name__

        # Login adapters (Delta, …) take a separate bridge-driven path.
        if adapter.requires_login:
            return await self._run_login_adapter(
                db, adapter, tender, portal, platform_slug, adapter_name,
                user_id, candidate_urls, task_id, resume_from_confirm,
            )

        cf = ContractsFinderDirectAdapter()
        is_cf_route = tender.source_code in CF_SOURCE_CODES or any(
            cf.matches_url(u) for u in candidate_urls
        )
        # Nothing to act on: no portal, no URLs, and not a CF tender. (CF
        # tenders with no public docs still resolve to a complete-but-empty
        # answer, which the adapter handles.)
        if portal is None and not candidate_urls and not is_cf_route:
            return OrchestrationResult(
                status=OrchestrationStatus.no_portal,
                tender_id=tender_id,
                detail="no non-deprecated portal with a link sighting",
            )

        portal_id = portal.id if portal else 0
        domain = portal.domain if portal else (
            urlparse(candidate_urls[0]).hostname if candidate_urls else ""
        )

        ctx = PortalContext(
            portal_id=portal_id,
            user_id=user_id,
            domain=domain or "",
            candidate_urls=candidate_urls,
            tender_id=tender.id,
            title=tender.title,
        )

        http_client: httpx.AsyncClient | None = None
        launched = None
        try:
            if adapter.requires_browser:
                launched = await self._launch_browser(portal_id, user_id)
                if launched is None:
                    return OrchestrationResult(
                        status=OrchestrationStatus.error,
                        tender_id=tender_id,
                        portal_id=portal_id or None,
                        platform_slug=platform_slug,
                        adapter=adapter_name,
                        detail="browser context unavailable",
                    )
                ctx.page = launched.page
            else:
                http_client = httpx.AsyncClient(
                    timeout=settings.http_timeout_seconds,
                    follow_redirects=True,
                    headers={"User-Agent": settings.http_user_agent},
                )
                ctx.http = http_client

            return await self._drive_adapter(
                db, adapter, ctx, tender, portal, platform_slug, adapter_name, user_id
            )
        finally:
            if http_client is not None:
                await http_client.aclose()
            if launched is not None:
                await launched.close()

    # --- login-via-human path (chunk 4) --------------------------------

    def _set_task(self, db: Session, task_id: str | None, **fields) -> None:
        if task_id is None:
            return
        task = db.execute(
            select(FetchTask).where(FetchTask.task_id == task_id)
        ).scalar_one_or_none()
        if task is None:
            return
        for k, v in fields.items():
            setattr(task, k, v)
        task.updated_at = datetime.now(UTC)
        db.commit()

    def _notify_waiting(self, db: Session, tender: Tender, platform_slug: str | None) -> None:
        try:
            base = settings.dashboard_base_url.rstrip("/")
            push.send_system_notification(
                db,
                title="Action needed: log in",
                body=(
                    f"Log in to {platform_slug or 'the portal'} to fetch documents "
                    f"for {tender.title}"
                ),
                url=f"{base}/tenders/{tender.id}",
                tag=f"login-{tender.id}",
            )
            db.commit()
        except Exception:  # noqa: BLE001
            logger.warning("orchestrator.notify_waiting_failed", tender_id=tender.id)

    async def _wait_for_login(
        self, bridge: BridgeClient, adapter: PortalAdapter, ctx: PortalContext, slug: str
    ) -> bool:
        """Re-poke the bridge in cycles until login is detected or the budget is
        exhausted. Waiting (not failing) while the user is away is deliberate."""
        import math

        cycle = max(1, self.login_wait_cycle_seconds)
        total = max(cycle, self.login_wait_total_seconds)
        iterations = max(1, math.ceil(total / cycle))
        pattern = adapter.login_success_pattern()
        for _ in range(iterations):
            try:
                res = await bridge.wait_for_login(
                    slug, pattern, login_url=adapter.login_url(), timeout_seconds=cycle
                )
            except Exception as exc:  # noqa: BLE001
                logger.info("orchestrator.wait_for_login_error", error=str(exc))
                res = {}
            if res.get("status") == "logged_in":
                return True
            try:
                if await adapter.is_authenticated(ctx):
                    return True
            except Exception:  # noqa: BLE001
                pass
        return False

    # --- session keepalive during the confirmation pause (chunk 4h) ----

    async def _session_keepalive(
        self,
        bridge: BridgeClient,
        slug: str,
        *,
        interval: float | None = None,
        budget: float | None = None,
    ) -> None:
        """Periodically touch the bridge session so Delta doesn't time it out
        while a fetch waits in needs_user_confirmation. Best-effort and bounded:
        stops at the budget (the resume re-auths anyway) or quietly the moment a
        ping fails (the session/bridge is gone)."""
        interval = interval if interval is not None else self.keepalive_interval_seconds
        budget = budget if budget is not None else self.keepalive_budget_seconds
        deadline = time.monotonic() + max(0.0, budget)
        pings = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(max(0.0, interval))
            if time.monotonic() >= deadline:
                break
            try:
                await bridge.session_status(slug)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.info("delta.session_keepalive_stopped", slug=slug, error=str(exc))
                return
            pings += 1
            logger.info("delta.session_keepalive", slug=slug, pings=pings)
        logger.info("delta.session_keepalive_budget_reached", slug=slug, pings=pings)

    def _start_keepalive(self, bridge: BridgeClient, slug: str) -> None:
        """Spawn the bounded keepalive as a background task. No-op when disabled,
        when no event loop is running, or when the bridge can't be pinged."""
        if not self.keepalive_enabled or not hasattr(bridge, "session_status"):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._stop_keepalive(slug)
        task = loop.create_task(self._session_keepalive(bridge, slug))
        self._keepalive_tasks[slug] = task

    def _stop_keepalive(self, slug: str) -> None:
        task = self._keepalive_tasks.pop(slug, None)
        if task is not None and not task.done():
            task.cancel()

    async def _run_login_adapter(
        self,
        db: Session,
        adapter: PortalAdapter,
        tender: Tender,
        portal: Portal | None,
        platform_slug: str | None,
        adapter_name: str,
        user_id: str,
        candidate_urls: list[str],
        task_id: str | None,
        resume_from_confirm: bool,
    ) -> OrchestrationResult:
        portal_id = portal.id if portal else None
        base = OrchestrationResult(
            status=OrchestrationStatus.error,
            tender_id=tender.id,
            portal_id=portal_id,
            platform_slug=platform_slug,
            adapter=adapter_name,
        )

        # Self-diagnosing preflight (issue 4d): on a real run — where the
        # orchestrator builds its own bridge — fail fast with an actionable
        # message instead of a raw PermissionError after the human has already
        # logged in, or a silent 401 from an unconfigured bridge token. Callers
        # that inject a bridge (unit tests) own their environment, so the guard
        # is skipped for them.
        if self._bridge is None:
            docs_ok, docs_detail = preflight.check_documents_dir_writable()
            if not docs_ok:
                logger.warning(
                    "orchestrator.preflight_failed",
                    check="documents_dir",
                    detail=docs_detail,
                )
                base.status = OrchestrationStatus.error
                base.detail = docs_detail
                return base
            token_ok, token_detail = preflight.check_bridge_token()
            if not token_ok:
                logger.warning(
                    "orchestrator.preflight_failed",
                    check="bridge_token",
                    detail=token_detail,
                )
                base.status = OrchestrationStatus.error
                base.detail = token_detail
                return base

        bridge = self._get_bridge()
        if not await bridge.bridge_available():
            base.status = OrchestrationStatus.bridge_unavailable
            base.detail = (
                "Browser bridge not running. Start start-bridge.ps1 on your PC, "
                "then retry."
            )
            return base

        slug = platform_slug or adapter.platform_slug or "portal"
        start_url = (
            next((u for u in candidate_urls if adapter.matches_url(u)), None)
            or (tender.source_url if tender.source_url else None)
            or adapter.login_url()
        )
        try:
            await bridge.open_session(slug, start_url)
        except Exception as exc:  # noqa: BLE001
            base.status = OrchestrationStatus.error
            base.detail = f"bridge open failed: {exc}"
            return base

        ctx = PortalContext(
            portal_id=portal_id or 0,
            user_id=user_id,
            domain=portal.domain if portal else "",
            candidate_urls=candidate_urls,
            tender_id=tender.id,
            bridge=bridge,
            platform_slug=slug,
            tender_ref=tender.source_ref,
            source_url=tender.source_url,
            description=tender.description,
            title=tender.title,
        )
        ref = tender.source_ref or str(tender.id)

        # The session is released (logout + close) on every terminal state so
        # the single Delta login is freed for the real user — EXCEPT the
        # needs_user_confirmation pause, which must keep the session alive for
        # the resume. The flag is cleared just before that one return.
        release_session = True
        try:
            # Concurrent-login guard at fetch start: if Delta is blocking us
            # because another login is active, fail fast with a clear, actionable
            # message instead of looping on waiting_for_login (issue 4e-2).
            conflict = await adapter.session_conflict(ctx)
            if conflict:
                logger.info(
                    "orchestrator.session_conflict",
                    platform=slug,
                    tender_id=tender.id,
                )
                base.status = OrchestrationStatus.failed
                base.detail = conflict
                return base

            if not resume_from_confirm:
                if await adapter.is_authenticated(ctx):
                    # Session reused: the persistent bridge context for this
                    # platform is already authenticated, so we skip
                    # waiting_for_login entirely. This is the common case once
                    # the human has logged in once (critical for Delta, where
                    # login needs Microsoft Authenticator).
                    logger.info(
                        "orchestrator.session_reused",
                        platform=slug,
                        tender_id=tender.id,
                    )
                else:
                    # Fresh login needed: the persistent bridge session for this
                    # platform isn't authenticated (first use, or it expired).
                    # The human logs in once in the visible window; thereafter
                    # the session is reused and this branch is skipped.
                    logger.info(
                        "orchestrator.fresh_login_required",
                        platform=slug,
                        tender_id=tender.id,
                    )
                    self._set_task(
                        db,
                        task_id,
                        status="waiting_for_login",
                        waiting_since=datetime.now(UTC),
                        login_url=adapter.login_url(),
                        bridge_session_slug=slug,
                        detail="Waiting for you to log in to the portal.",
                    )
                    self._notify_waiting(db, tender, platform_slug)
                    if not await self._wait_for_login(bridge, adapter, ctx, slug):
                        base.status = OrchestrationStatus.failed
                        base.detail = "Login not completed — retry when you're ready."
                        return base
                    self._set_task(db, task_id, status="running", detail=None)

                locate = await adapter.locate_tender(ctx, ref)
                if locate.status == LocateStatus.requires_interest_first:
                    if locate.already_authorized and not self.pause_on_already_registered:
                        # Already registered: there is NO intent-signalling
                        # Register Interest click to gate, so we don't pause — we
                        # just read documents the user already has access to. This
                        # also sidesteps the confirm-delay session timeout for the
                        # proven already-registered path (chunk 4h, Fix 7).
                        logger.info(
                            "orchestrator.already_registered_no_pause",
                            platform=slug, tender_id=tender.id,
                        )
                        reg = await adapter.register_interest(ctx)
                        if reg.status not in (
                            RegisterStatus.success,
                            RegisterStatus.already_registered,
                        ):
                            base.status = OrchestrationStatus.register_failed
                            base.detail = reg.detail
                            return base
                        # fall through to the shared download block.
                    else:
                        self._set_task(
                            db,
                            task_id,
                            status="needs_user_confirmation",
                            bridge_session_slug=slug,
                            login_url=locate.tender_url,
                            detail=(
                                locate.detail
                                or "Express Interest is required before documents "
                                "are released."
                            ),
                        )
                        base.status = OrchestrationStatus.needs_user_confirmation
                        base.detail = locate.detail
                        # Keep the session alive across the pause so the resume can
                        # reuse it (re-login needs Microsoft Authenticator), and
                        # ping it periodically so Delta doesn't time it out before
                        # the user confirms (chunk 4h, Fix 6).
                        release_session = False
                        self._start_keepalive(bridge, slug)
                        return base
                elif locate.status != LocateStatus.found:
                    base.status = OrchestrationStatus.locate_failed
                    base.detail = locate.detail or locate.status.value
                    return base
            else:
                # Resuming after the user confirmed Express Interest. The
                # confirm may have come minutes later, so the Delta session can
                # have timed out during the pause. Re-auth instead of hard-failing
                # (chunk 4h, Fix 5): an expired session becomes "log in again",
                # reusing the waiting_for_login machinery.
                self._stop_keepalive(slug)
                if not await adapter.is_authenticated(ctx):
                    logger.info(
                        "orchestrator.session_expired_on_resume",
                        platform=slug, tender_id=tender.id,
                    )
                    self._set_task(
                        db,
                        task_id,
                        status="waiting_for_login",
                        waiting_since=datetime.now(UTC),
                        login_url=adapter.login_url(),
                        bridge_session_slug=slug,
                        detail="Session expired during the pause — log in again to continue.",
                    )
                    self._notify_waiting(db, tender, platform_slug)
                    if not await self._wait_for_login(bridge, adapter, ctx, slug):
                        base.status = OrchestrationStatus.failed
                        base.detail = "Login not completed — retry when you're ready."
                        return base
                    self._set_task(db, task_id, status="running", detail=None)
                await adapter.locate_tender(ctx, ref)
                reg = await adapter.register_interest(ctx)
                if reg.status not in (
                    RegisterStatus.success,
                    RegisterStatus.already_registered,
                ):
                    base.status = OrchestrationStatus.register_failed
                    base.detail = reg.detail
                    return base
                await adapter.locate_tender(ctx, ref)

            dest = _dest_dir(tender.id)
            download = await adapter.download_documents(ctx, dest)
            base.files = [
                {
                    "url": f.url,
                    "filename": f.filename,
                    "bytes": f.bytes,
                    "content_type": f.content_type,
                    "sha256": f.sha256,
                }
                for f in download.files
            ]
            base.missing = list(download.missing)
            base.files_persisted = self._persist_documents(db, tender, download)
            if download.status == DownloadStatus.complete:
                base.status = OrchestrationStatus.complete
            elif download.status == DownloadStatus.partial:
                base.status = OrchestrationStatus.partial
            elif download.status == DownloadStatus.nothing_available:
                base.status = OrchestrationStatus.nothing_available
            else:
                base.status = OrchestrationStatus.download_failed
                base.detail = download.detail
            return base
        finally:
            if release_session:
                await self._release_session(bridge, adapter, ctx, slug, tender.id)

    async def _release_session(
        self,
        bridge: BridgeClient,
        adapter: PortalAdapter,
        ctx: PortalContext,
        slug: str,
        tender_id: int,
    ) -> None:
        """Release the portal session after a login-adapter fetch reaches a
        terminal state. Delta enforces a single concurrent login per account, so
        holding the session locks the real user out (the lockout seen in live
        testing — issue 4e-2). We log out server-side (best-effort) THEN close
        the local bridge context. Both steps are non-fatal."""
        with contextlib.suppress(Exception):
            await adapter.logout(ctx)
        with contextlib.suppress(Exception):
            await bridge.close_session(slug)
        logger.info("delta.session_released", platform=slug, tender_id=tender_id)

    def _load_credentials(self, portal_id: int, user_id: str) -> Credentials | None:
        store = self._store()
        if store is None:
            return None
        try:
            return store.get_credentials(portal_id, user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("orchestrator.creds_load_failed", error=str(exc))
            return None

    async def _launch_browser(self, portal_id: int, user_id: str):
        try:
            from tender_agent.services.browser import BrowserContextManager

            mgr = BrowserContextManager(headless=True)
            return await mgr.launch(user_id, portal_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("orchestrator.browser_launch_failed", error=str(exc))
            return None

    async def _drive_adapter(
        self,
        db: Session,
        adapter: PortalAdapter,
        ctx: PortalContext,
        tender: Tender,
        portal: Portal | None,
        platform_slug: str | None,
        adapter_name: str,
        user_id: str,
    ) -> OrchestrationResult:
        portal_id = portal.id if portal else None
        base = OrchestrationResult(
            status=OrchestrationStatus.error,
            tender_id=tender.id,
            portal_id=portal_id,
            platform_slug=platform_slug,
            adapter=adapter_name,
        )

        creds = (
            self._load_credentials(portal_id, user_id)
            if portal_id is not None
            else None
        )
        auth = await adapter.authenticate(ctx, creds)
        if auth.status == AuthStatus.needs_registration:
            base.status = OrchestrationStatus.needs_registration
            base.detail = auth.detail
            return base
        if auth.status == AuthStatus.invalid_credentials:
            store = self._store()
            if store is not None and portal_id is not None:
                try:
                    store.mark_invalid(portal_id, user_id)
                except Exception:  # noqa: BLE001
                    logger.warning("orchestrator.mark_invalid_failed")
            base.status = OrchestrationStatus.credentials_invalid
            base.detail = auth.detail
            return base
        if auth.status == AuthStatus.requires_2fa:
            base.status = OrchestrationStatus.needs_2fa
            base.detail = auth.detail
            return base
        if auth.status in (AuthStatus.blocked, AuthStatus.error):
            base.status = OrchestrationStatus.blocked
            base.detail = auth.detail
            return base

        tender_ref = tender.source_ref or str(tender.id)
        locate = await adapter.locate_tender(ctx, tender_ref)
        if locate.status == LocateStatus.requires_interest_first:
            reg = await adapter.register_interest(ctx)
            if reg.status not in (
                RegisterStatus.success,
                RegisterStatus.already_registered,
            ):
                base.status = OrchestrationStatus.register_failed
                base.detail = reg.detail
                return base
            locate = await adapter.locate_tender(ctx, tender_ref)
        if locate.status != LocateStatus.found:
            base.status = OrchestrationStatus.locate_failed
            base.detail = locate.detail or locate.status.value
            return base

        dest = _dest_dir(tender.id)
        download = await adapter.download_documents(ctx, dest)
        base.files = [
            {
                "url": f.url,
                "filename": f.filename,
                "bytes": f.bytes,
                "content_type": f.content_type,
                "sha256": f.sha256,
            }
            for f in download.files
        ]
        base.missing = list(download.missing)
        base.files_persisted = self._persist_documents(db, tender, download)
        if download.status == DownloadStatus.complete:
            base.status = OrchestrationStatus.complete
        elif download.status == DownloadStatus.partial:
            base.status = OrchestrationStatus.partial
        elif download.status == DownloadStatus.nothing_available:
            base.status = OrchestrationStatus.nothing_available
        else:
            base.status = OrchestrationStatus.download_failed
            base.detail = download.detail

        store = self._store()
        if (
            store is not None
            and portal_id is not None
            and base.status
            in (OrchestrationStatus.complete, OrchestrationStatus.partial)
        ):
            with contextlib.suppress(Exception):
                store.mark_used(portal_id, user_id)
        return base

    def _persist_documents(
        self, db: Session, tender: Tender, download: DownloadResult
    ) -> int:
        """Persist downloaded files as TenderDocumentFile rows, deduped by
        sha256 so re-running fetch never duplicates. Files without a sha256
        (e.g. from non-persisting adapters in tests) are skipped.

        After persistence, extract document text into the content store so the
        content becomes durable, reusable database data (extract once, reuse
        forever — including across tenders by sha256). Extraction failures for
        individual documents are recorded as content rows with status='error'
        and MUST NOT fail the fetch.
        """
        persisted = 0
        for f in download.files:
            if not f.sha256 or not f.storage_key:
                continue
            existing_sha = db.execute(
                select(TenderDocumentFile)
                .where(TenderDocumentFile.tender_id == tender.id)
                .where(TenderDocumentFile.sha256 == f.sha256)
                .limit(1)
            ).scalar_one_or_none()
            if existing_sha is not None:
                persisted += 1
                continue
            by_url = db.execute(
                select(TenderDocumentFile)
                .where(TenderDocumentFile.tender_id == tender.id)
                .where(TenderDocumentFile.url == f.url)
                .limit(1)
            ).scalar_one_or_none()
            rec = by_url or TenderDocumentFile(tender_id=tender.id, url=f.url)
            rec.title = f.filename
            rec.format = (
                (f.content_type or "").split("/")[-1].split("+")[0] or None
            )
            rec.storage_key = f.storage_key
            rec.storage_backend = "local"
            rec.bytes = f.bytes
            rec.sha256 = f.sha256
            rec.download_status = "ok"
            rec.error = None
            rec.downloaded_at = datetime.now(UTC)
            if by_url is None:
                db.add(rec)
            db.flush()
            persisted += 1
        db.commit()

        # Capture content at fetch time so re-fetches and brief-generation
        # both reuse it without re-reading the file. Local import to avoid a
        # circular import (brief services touch the orchestrator's models).
        try:
            from tender_agent.services.brief.content_store import (
                ensure_content_extracted,
            )

            db.refresh(tender)
            ensure_content_extracted(db, tender)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "orchestrator.content_extract_failed",
                tender_id=tender.id,
                error=str(exc),
            )

        return persisted
