"""FallbackAdapter — the catch-all for portals without a dedicated adapter.

It performs no login (none is possible without portal-specific knowledge) and
attempts to download whatever is publicly reachable over plain HTTP. Anything
that 401/403s or redirects to a login page is reported as missing, flagging
"this lives behind a login" to the user.

This is the only adapter that ships a real implementation in chunk 2, so the
orchestrator's happy path is exercisable end-to-end without Playwright.
"""
from __future__ import annotations

import os
import re
from urllib.parse import urlparse

import httpx
import structlog

from tender_agent.config import settings
from tender_agent.services.portals.base import (
    Credentials,
    PortalAdapter,
    PortalContext,
)
from tender_agent.services.portals.results import (
    AuthResult,
    AuthStatus,
    DownloadedFile,
    DownloadResult,
    DownloadStatus,
    LocateResult,
    LocateStatus,
    RegisterResult,
    RegisterStatus,
)

logger = structlog.get_logger(__name__)

# Heuristic: a response that's HTML and mentions a login form is gated.
_LOGIN_MARKERS = re.compile(
    r"(sign in|log in|login|password|username|register interest)", re.IGNORECASE
)
_MAX_BYTES = 50 * 1024 * 1024


class FallbackAdapter(PortalAdapter):
    platform_slug = None
    requires_browser = False

    def matches_url(self, url: str) -> bool:
        # Catch-all: matches everything as the last resort.
        return True

    async def authenticate(
        self, ctx: PortalContext, creds: Credentials | None
    ) -> AuthResult:
        # No login attempted; we only fetch public resources.
        return AuthResult(status=AuthStatus.success, detail="no auth (fallback)")

    async def locate_tender(
        self, ctx: PortalContext, tender_ref: str
    ) -> LocateResult:
        # The fallback can't navigate a portal UI; it relies on the document
        # URLs handed to download_documents, so "found" just lets the flow
        # proceed to the download attempt.
        return LocateResult(status=LocateStatus.found, detail="fallback (no nav)")

    async def register_interest(self, ctx: PortalContext) -> RegisterResult:
        # Registering interest requires portal-specific automation we don't
        # have here.
        return RegisterResult(
            status=RegisterStatus.error,
            detail="register_interest unsupported by FallbackAdapter",
        )

    async def download_documents(
        self, ctx: PortalContext, dest_dir: str
    ) -> DownloadResult:
        candidate_urls = list(ctx.candidate_urls or [])
        if not candidate_urls:
            return DownloadResult(
                status=DownloadStatus.nothing_available,
                detail="no candidate document URLs",
            )

        os.makedirs(dest_dir, exist_ok=True)
        client = ctx.http
        owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(
                timeout=settings.http_timeout_seconds,
                follow_redirects=True,
                headers={"User-Agent": settings.http_user_agent},
            )
        files: list[DownloadedFile] = []
        missing: list[str] = []
        try:
            for url in candidate_urls:
                ok, df = await self._try_download(client, url, dest_dir)
                if ok and df is not None:
                    files.append(df)
                else:
                    missing.append(url)
        finally:
            if owns_client:
                await client.aclose()

        if files and missing:
            status = DownloadStatus.partial
        elif files:
            status = DownloadStatus.complete
        else:
            status = DownloadStatus.nothing_available
        return DownloadResult(status=status, files=files, missing=missing)

    async def _try_download(
        self, client: httpx.AsyncClient, url: str, dest_dir: str
    ) -> tuple[bool, DownloadedFile | None]:
        try:
            resp = await client.get(url)
        except Exception as exc:  # noqa: BLE001
            logger.info("fallback.download_error", url=url, error=str(exc))
            return False, None
        if resp.status_code in (401, 403):
            return False, None
        if resp.status_code >= 400:
            return False, None
        content_type = resp.headers.get("content-type", "")
        body = resp.content
        if len(body) > _MAX_BYTES:
            return False, None
        # If it's an HTML page that looks like a login wall, treat as gated.
        if "text/html" in content_type and _LOGIN_MARKERS.search(
            resp.text[:4000]
        ):
            return False, None
        filename = _filename_from_url(url) or "document"
        path = os.path.join(dest_dir, filename)
        try:
            with open(path, "wb") as fh:
                fh.write(body)
        except OSError as exc:
            logger.warning("fallback.write_failed", url=url, error=str(exc))
            return False, None
        return True, DownloadedFile(
            url=url,
            path=path,
            filename=filename,
            bytes=len(body),
            content_type=content_type or None,
        )


def _filename_from_url(url: str) -> str | None:
    path = urlparse(url).path
    name = os.path.basename(path)
    return name or None
