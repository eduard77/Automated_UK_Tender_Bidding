"""ContractsFinderDirectAdapter — the first real adapter.

Contracts Finder publishes documents on the public gov asset host
``assets.publishing.service.gov.uk``. Those files need no login and no
browser, so this adapter fetches them with plain httpx.

Security boundary: the adapter ONLY fetches URLs whose host matches its
``DOMAIN_PATTERNS``. Anything else (buyer-portal links that need a login,
arbitrary external URLs) is rejected and reported as missing — never fetched.

Note on the data reality: Contracts Finder's OCDS feed exposes tender
documents as HTML *notice-page* links (on contractsfinder.service.gov.uk),
not as direct file attachments, and those notices are a JS SPA. Genuine
tender attachments live behind buyer portals (chunk 4+). So in practice a
real CF tender often yields zero asset-host files here — a complete-but-empty
result, which is the honest answer ("no public documents from CF").
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
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

MAX_FILE_BYTES = 100 * 1024 * 1024  # 100 MB per document
MAX_DOCS = 50  # per tender


def secure_filename(name: str) -> str:
    """Strip path components, illegal characters, and leading dots.

    Self-contained (no werkzeug dependency). Mirrors werkzeug.secure_filename's
    intent: the result never contains a path separator and never starts with a
    dot, so it can't escape the destination directory or create a dotfile.
    """
    # Drop any directory components from both separators.
    name = name.replace("\\", "/").split("/")[-1]
    # Keep a conservative character set.
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    # Collapse runs of underscores/dots and strip leading dots/underscores.
    name = re.sub(r"_{2,}", "_", name).lstrip("._")
    if not name:
        name = "document"
    # Cap length to keep filesystems happy.
    return name[:200]


def _ext_from(url: str, content_type: str | None) -> str:
    path = urlparse(url).path
    if "." in path:
        cand = path.rsplit(".", 1)[-1].lower()
        if cand.isalnum() and len(cand) <= 8:
            return cand
    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        mapping = {
            "application/pdf": "pdf",
            "application/msword": "doc",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
            "application/vnd.ms-excel": "xls",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
            "text/html": "html",
            "application/zip": "zip",
        }
        if ct in mapping:
            return mapping[ct]
    return "bin"


class ContractsFinderDirectAdapter(PortalAdapter):
    platform_slug = "contracts_finder_direct"
    requires_browser = False

    DOMAIN_PATTERNS = [r"(^|\.)assets\.publishing\.service\.gov\.uk$"]

    def matches_url(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        if not host:
            return False
        return any(re.search(p, host) for p in self.DOMAIN_PATTERNS)

    async def authenticate(
        self, ctx: PortalContext, creds: Credentials | None
    ) -> AuthResult:
        # Public documents — no login.
        return AuthResult(status=AuthStatus.success, detail="public (no auth)")

    async def locate_tender(
        self, ctx: PortalContext, tender_ref: str
    ) -> LocateResult:
        return LocateResult(status=LocateStatus.found, detail="direct asset host")

    async def register_interest(self, ctx: PortalContext) -> RegisterResult:
        return RegisterResult(
            status=RegisterStatus.error,
            detail="register_interest not applicable to CF direct",
        )

    async def download_documents(
        self, ctx: PortalContext, dest_dir: str
    ) -> DownloadResult:
        candidates = list(ctx.candidate_urls or [])
        # Security boundary: only ever fetch allowlisted hosts.
        allowed = [u for u in candidates if self.matches_url(u)]
        rejected = [u for u in candidates if not self.matches_url(u)]
        if rejected:
            logger.info(
                "cf_adapter.rejected_untrusted",
                count=len(rejected),
                sample=rejected[:3],
            )

        if not allowed:
            # Nothing public to fetch. CF is authoritative: this is a complete
            # (if empty) answer, with any portal URLs flagged as missing.
            return DownloadResult(
                status=DownloadStatus.complete,
                files=[],
                missing=rejected,
                detail="no assets.publishing.service.gov.uk documents",
            )

        capped = allowed[:MAX_DOCS]
        over_cap = allowed[MAX_DOCS:]

        client = ctx.http
        owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(
                timeout=settings.http_timeout_seconds,
                follow_redirects=True,
                headers={"User-Agent": settings.http_user_agent},
            )
        files: list[DownloadedFile] = []
        missing: list[str] = list(rejected) + list(over_cap)
        scope = ctx.tender_id or ctx.portal_id
        try:
            for url in capped:
                df = await self._download_one(client, url, scope)
                if df is not None:
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
            # All allowed URLs failed to fetch.
            status = DownloadStatus.partial if missing else DownloadStatus.complete
        return DownloadResult(status=status, files=files, missing=missing)

    async def _download_one(
        self, client: httpx.AsyncClient, url: str, tender_or_portal_id: int
    ) -> DownloadedFile | None:
        try:
            async with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    logger.info("cf_adapter.http_error", url=url, status=resp.status_code)
                    return None
                content_type = (
                    resp.headers.get("Content-Type", "").split(";")[0].strip() or None
                )
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > MAX_FILE_BYTES:
                        logger.info("cf_adapter.oversize", url=url)
                        return None
        except Exception as exc:  # noqa: BLE001
            logger.info("cf_adapter.fetch_failed", url=url, error=str(exc))
            return None

        data = bytes(buf)
        sha = hashlib.sha256(data).hexdigest()
        ext = _ext_from(url, content_type)
        base_name = secure_filename(os.path.basename(urlparse(url).path) or f"document.{ext}")
        if "." not in base_name:
            base_name = f"{base_name}.{ext}"

        # Storage layout matches services/document_downloader: sha-sharded under
        # the document storage root, keyed by tender/portal scope.
        root = Path(settings.document_storage_dir)
        rel = Path(str(tender_or_portal_id)) / sha[:2] / f"{sha}.{ext}"
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(data)

        return DownloadedFile(
            url=url,
            path=str(target),
            filename=base_name,
            bytes=len(data),
            content_type=content_type,
            sha256=sha,
            storage_key=str(rel),
        )
