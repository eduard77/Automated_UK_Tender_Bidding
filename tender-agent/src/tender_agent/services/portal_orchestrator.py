"""PortalOrchestrator — the 'click Generate brief' flow up to documents
downloaded.

This prompt wires the full control flow and makes it work end-to-end for the
FallbackAdapter (the only real adapter shipping in chunk 2). Real platform
adapters plug in via services/portals/registry.ADAPTERS in later prompts.
"""
from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.config import settings
from tender_agent.models import Portal, PortalUrlSighting, Tender
from tender_agent.services.credentials import CredentialsStore
from tender_agent.services.portals.base import (
    Credentials,
    PortalAdapter,
    PortalContext,
)
from tender_agent.services.portals.registry import (
    get_adapter_for_platform,
    get_fallback_adapter,
)
from tender_agent.services.portals.results import (
    AuthStatus,
    DownloadStatus,
    LocateStatus,
    RegisterStatus,
)

logger = structlog.get_logger(__name__)


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
    detail: str | None = None


def _dest_dir(tender_id: int) -> str:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = os.environ.get("TENDER_AGENT_DOC_ROOT") or os.path.join(
        "data", "tender-documents"
    )
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
    ) -> None:
        self._creds_store = credentials_store

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
        self, tender_id: int, user_id: str = "eduard", db: Session | None = None
    ) -> OrchestrationResult:
        if db is None:
            from tender_agent.db import SessionLocal

            with SessionLocal() as owned_db:
                return await self._run(tender_id, user_id, owned_db)
        return await self._run(tender_id, user_id, db)

    async def _run(
        self, tender_id: int, user_id: str, db: Session
    ) -> OrchestrationResult:
        tender = db.get(Tender, tender_id)
        if tender is None:
            return OrchestrationResult(
                status=OrchestrationStatus.error,
                tender_id=tender_id,
                detail="tender not found",
            )

        portal, sightings = self._select_portal(db, tender_id)
        if portal is None:
            return OrchestrationResult(
                status=OrchestrationStatus.no_portal,
                tender_id=tender_id,
                detail="no non-deprecated portal with a link sighting",
            )

        platform_slug = portal.platform.slug if portal.platform else None
        adapter_cls = get_adapter_for_platform(platform_slug) or get_fallback_adapter()
        adapter: PortalAdapter = adapter_cls()
        adapter_name = adapter_cls.__name__

        ctx = PortalContext(
            portal_id=portal.id,
            user_id=user_id,
            domain=portal.domain,
            candidate_urls=_candidate_urls(tender, sightings),
        )

        http_client: httpx.AsyncClient | None = None
        launched = None
        try:
            if adapter.requires_browser:
                launched = await self._launch_browser(portal.id, user_id)
                if launched is None:
                    return OrchestrationResult(
                        status=OrchestrationStatus.error,
                        tender_id=tender_id,
                        portal_id=portal.id,
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
                adapter, ctx, tender, portal, platform_slug, adapter_name, user_id
            )
        finally:
            if http_client is not None:
                await http_client.aclose()
            if launched is not None:
                await launched.close()

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
        adapter: PortalAdapter,
        ctx: PortalContext,
        tender: Tender,
        portal: Portal,
        platform_slug: str | None,
        adapter_name: str,
        user_id: str,
    ) -> OrchestrationResult:
        base = OrchestrationResult(
            status=OrchestrationStatus.error,
            tender_id=tender.id,
            portal_id=portal.id,
            platform_slug=platform_slug,
            adapter=adapter_name,
        )

        creds = self._load_credentials(portal.id, user_id)
        auth = await adapter.authenticate(ctx, creds)
        if auth.status == AuthStatus.needs_registration:
            base.status = OrchestrationStatus.needs_registration
            base.detail = auth.detail
            return base
        if auth.status == AuthStatus.invalid_credentials:
            store = self._store()
            if store is not None:
                try:
                    store.mark_invalid(portal.id, user_id)
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
                "path": f.path,
                "filename": f.filename,
                "bytes": f.bytes,
                "content_type": f.content_type,
            }
            for f in download.files
        ]
        base.missing = list(download.missing)
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
        if store is not None and base.status in (
            OrchestrationStatus.complete,
            OrchestrationStatus.partial,
        ):
            with contextlib.suppress(Exception):
                store.mark_used(portal.id, user_id)
        return base
