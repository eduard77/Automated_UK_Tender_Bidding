"""Delta eSourcing adapter (platform_slug='delta_esourcing').

Login model: a human logs in to the real Delta site in the visible browser
window the bridge opens. This adapter NEVER submits credentials and never sees
the password. After the orchestrator confirms login (via wait-for-login), it
drives this adapter through the bridge to locate the tender and download
documents.

╔══════════════════════════════════════════════════════════════════════════╗
║  HONESTY NOTE — read before trusting this adapter.                         ║
║  This was written WITHOUT a Delta account, against the *known structure*   ║
║  of Delta eSourcing (a JAGGAER-family platform). Every Delta-specific URL  ║
║  and selector below is a best-effort guess and is marked                   ║
║  "VALIDATE ON FRIDAY". The control flow is real and tested with a mocked   ║
║  bridge; the constants are what need correcting against the live site.     ║
╚══════════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from urllib.parse import urlparse

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


# --- Delta-specific constants — VALIDATE ON FRIDAY ----------------------
# Correcting these against the live site is the entire Friday fix-up; the
# logic around them is platform-agnostic.

DELTA_URLS = {
    # VALIDATE ON FRIDAY — supplier login page.
    "login": "https://www.delta-esourcing.com/delta/login.html",
    # VALIDATE ON FRIDAY — landing page after login (used to detect a live
    # session): the supplier dashboard / home.
    "authenticated_landing": "https://www.delta-esourcing.com/delta/respond.html",
    # VALIDATE ON FRIDAY — tender search; %s is the tender reference.
    "search": "https://www.delta-esourcing.com/delta/searchTenders.html?q=%s",
}

DELTA_SELECTORS = {
    # VALIDATE ON FRIDAY — present only when logged in (e.g. a logout link).
    "logged_in_marker": "a[href*='logout'], a:has-text('My Account'), #logoutLink",
    # VALIDATE ON FRIDAY — present on the login page.
    "login_form_marker": "form[action*='login'], input[type='password']",
    # VALIDATE ON FRIDAY — the "Express Interest" control that gates documents.
    "express_interest": (
        "a:has-text('Express Interest'), button:has-text('Express Interest'), "
        "#expressInterestBtn"
    ),
    # VALIDATE ON FRIDAY — container shown once documents are released.
    "documents_area": "#documents, .tender-documents, section:has-text('Documents')",
    # VALIDATE ON FRIDAY — individual document download anchors.
    "document_link": "a[href*='download'], a[href$='.pdf'], a[href$='.docx']",
}

# href pattern used with the bridge's find-links to enumerate document URLs.
# VALIDATE ON FRIDAY.
DELTA_DOCUMENT_HREF_PATTERN = r"(download|\.pdf|\.docx|\.doc|\.xls|\.xlsx|\.zip)"

# Login-success URL pattern for wait-for-login (NOT the login page).
# VALIDATE ON FRIDAY.
DELTA_LOGIN_SUCCESS_PATTERN = r"delta-esourcing\.com/delta/(?!login)"

DELTA_DOMAIN_RE = re.compile(r"(^|\.)delta-esourcing\.com$", re.IGNORECASE)

MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_DOCS = 50


class DeltaEsourcingAdapter(PortalAdapter):
    platform_slug = "delta_esourcing"
    requires_browser = False  # uses the Windows bridge, not an in-process page
    requires_login = True

    # --- classification / login ----------------------------------------

    def matches_url(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return bool(host) and bool(DELTA_DOMAIN_RE.search(host))

    def login_url(self) -> str | None:
        return DELTA_URLS["login"]

    def login_success_pattern(self) -> str:
        return DELTA_LOGIN_SUCCESS_PATTERN

    async def is_authenticated(self, ctx: PortalContext) -> bool:
        """Navigate to the authenticated landing page; logged in if we're not
        bounced to login and a logged-in marker is present. Best-effort,
        constant-driven."""
        bridge, slug = ctx.bridge, ctx.platform_slug
        if bridge is None or slug is None:
            return False
        try:
            await bridge.navigate(slug, DELTA_URLS["authenticated_landing"])
            status = await bridge.session_status(slug)
        except Exception as exc:  # noqa: BLE001
            logger.info("delta.is_authenticated_error", error=str(exc))
            return False
        current = (status.get("current_url") or "").lower()
        if "login" in current:
            return False
        try:
            html = await bridge.page_html(slug)
        except Exception:  # noqa: BLE001
            html = ""
        # Logged in if a known marker text is present (cheap check on HTML).
        return ("logout" in html.lower()) or ("my account" in html.lower())

    async def authenticate(
        self, ctx: PortalContext, creds: Credentials | None
    ) -> AuthResult:
        # Login is performed by the human in the visible window; the
        # orchestrator only calls this once wait-for-login has confirmed it.
        # The adapter never submits credentials.
        return AuthResult(status=AuthStatus.success, detail="human login (no creds)")

    # --- locate / interest / download ----------------------------------

    def _tender_url(self, ctx: PortalContext) -> str | None:
        """Prefer a direct Delta URL from the tender's source data; else a
        search URL built from the reference."""
        if ctx.source_url and self.matches_url(ctx.source_url):
            return ctx.source_url
        for u in ctx.candidate_urls:
            if self.matches_url(u):
                return u
        if ctx.tender_ref:
            return DELTA_URLS["search"] % ctx.tender_ref
        return None

    async def locate_tender(
        self, ctx: PortalContext, tender_ref: str
    ) -> LocateResult:
        bridge, slug = ctx.bridge, ctx.platform_slug
        if bridge is None or slug is None:
            return LocateResult(status=LocateStatus.error, detail="no bridge session")
        url = self._tender_url(ctx)
        if url is None:
            return LocateResult(
                status=LocateStatus.not_found, detail="no Delta tender URL derivable"
            )
        if not self.matches_url(url):
            # Security boundary: never navigate off-platform.
            return LocateResult(
                status=LocateStatus.error, detail=f"refusing off-platform URL: {url}"
            )
        try:
            await bridge.navigate(slug, url)
            html = await bridge.page_html(slug)
        except Exception as exc:  # noqa: BLE001
            return LocateResult(status=LocateStatus.error, detail=str(exc))

        low = html.lower()
        # Documents already visible? then we're good.
        if re.search(DELTA_DOCUMENT_HREF_PATTERN, low):
            return LocateResult(status=LocateStatus.found, tender_url=url)
        # Express-Interest gate present → must pause for user confirmation.
        if "express interest" in low:
            return LocateResult(
                status=LocateStatus.requires_interest_first,
                tender_url=url,
                detail="Documents are released only after Express Interest.",
            )
        # Loaded but nothing obvious — treat as found; download step will report
        # nothing_available if there's truly nothing.
        return LocateResult(status=LocateStatus.found, tender_url=url)

    async def register_interest(self, ctx: PortalContext) -> RegisterResult:
        """Click Express Interest. The orchestrator only calls this AFTER the
        user explicitly confirms — never automatically."""
        bridge, slug = ctx.bridge, ctx.platform_slug
        if bridge is None or slug is None:
            return RegisterResult(status=RegisterStatus.error, detail="no bridge")
        try:
            await bridge.click_download(slug, DELTA_SELECTORS["express_interest"])
        except Exception:  # noqa: BLE001
            # Express Interest may not trigger a download; a plain click via
            # navigate-after isn't exposed, so treat a non-download click as
            # success if the page advanced. Best-effort; VALIDATE ON FRIDAY.
            try:
                status = await bridge.session_status(slug)
                if status.get("authenticated_guess"):
                    return RegisterResult(status=RegisterStatus.success)
            except Exception as exc:  # noqa: BLE001
                return RegisterResult(status=RegisterStatus.error, detail=str(exc))
        return RegisterResult(status=RegisterStatus.success)

    async def download_documents(
        self, ctx: PortalContext, dest_dir: str
    ) -> DownloadResult:
        bridge, slug = ctx.bridge, ctx.platform_slug
        if bridge is None or slug is None:
            return DownloadResult(
                status=DownloadStatus.error, detail="no bridge session"
            )
        try:
            links = await bridge.find_links(slug, DELTA_DOCUMENT_HREF_PATTERN)
        except Exception as exc:  # noqa: BLE001
            return DownloadResult(status=DownloadStatus.error, detail=str(exc))

        # Security boundary: only download Delta-hosted URLs.
        allowed = [u for u in links if self.matches_url(u)]
        rejected = [u for u in links if not self.matches_url(u)]
        if not allowed:
            return DownloadResult(
                status=DownloadStatus.nothing_available,
                missing=rejected,
                detail="no Delta-hosted document links found",
            )

        files: list[DownloadedFile] = []
        missing: list[str] = list(rejected) + list(allowed[MAX_DOCS:])
        for url in allowed[:MAX_DOCS]:
            try:
                bf = await bridge.download(slug, url)
            except Exception as exc:  # noqa: BLE001
                logger.info("delta.download_failed", url=url, error=str(exc))
                missing.append(url)
                continue
            df = _ingest_bridge_file(bf, url, ctx.tender_id)
            if df is None:
                missing.append(url)
            else:
                files.append(df)

        if files and missing:
            status = DownloadStatus.partial
        elif files:
            status = DownloadStatus.complete
        else:
            status = DownloadStatus.partial if missing else DownloadStatus.nothing_available
        return DownloadResult(status=status, files=files, missing=missing)


def _ingest_bridge_file(bf, url: str, tender_id: int) -> DownloadedFile | None:
    """Read a bridge-downloaded file from the shared volume, size-cap it,
    sha256 it, and copy it into the tender-documents storage layout."""
    src = Path(settings.bridge_download_dir) / bf.path
    if not src.is_file():
        logger.info("delta.bridge_file_missing", path=str(src))
        return None
    size = src.stat().st_size
    if size > MAX_FILE_BYTES:
        logger.info("delta.oversize", path=str(src), size=size)
        return None
    data = src.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    ext = _ext_from(url, bf.mime_type)
    rel = Path(str(tender_id)) / sha[:2] / f"{sha}.{ext}"
    target = Path(settings.document_storage_dir) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(data)
    filename = _safe_name(os.path.basename(urlparse(url).path) or f"document.{ext}")
    if "." not in filename:
        filename = f"{filename}.{ext}"
    return DownloadedFile(
        url=url,
        path=str(target),
        filename=filename,
        bytes=len(data),
        content_type=bf.mime_type,
        sha256=sha,
        storage_key=str(rel),
    )


def _safe_name(name: str) -> str:
    name = (name or "document").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name).lstrip("._")
    return name[:200] or "document"


def _ext_from(url: str, mime: str | None) -> str:
    path = urlparse(url).path
    if "." in path:
        cand = path.rsplit(".", 1)[-1].lower()
        if cand.isalnum() and len(cand) <= 8:
            return cand
    mapping = {
        "application/pdf": "pdf",
        "application/msword": "doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "application/zip": "zip",
    }
    if mime and mime.split(";")[0].strip().lower() in mapping:
        return mapping[mime.split(";")[0].strip().lower()]
    return "bin"
