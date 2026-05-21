"""Delta eSourcing adapter (platform_slug='delta_esourcing').

Login model: a human logs in to the real Delta site in the visible browser
window the bridge opens — password AND Microsoft Authenticator (app-based 2FA),
entirely by hand. This adapter NEVER submits credentials or 2FA codes and never
sees the password. Because of the authenticator, the persistent bridge session
is reused across fetches so the human re-authenticates as rarely as possible.

╔══════════════════════════════════════════════════════════════════════════╗
║  REAL-FLOW NOTE (corrected from chunk 4 after live reconnaissance).        ║
║  Delta has NO supplier-side tender search. The entry path is the           ║
║  Response Manager: the supplier types the tender's *access code* (which     ║
║  comes from the FTS/CF notice we already ingest) into the "Access Code"     ║
║  box and clicks Submit. If the tender is open it lands in the supplier's    ║
║  "Responses" table and documents become reachable; if closed Delta shows   ║
║  the literal banner "This opportunity is not currently open."              ║
║                                                                            ║
║  CONFIRMED constants below are validated against the live site. Selectors  ║
║  marked "CONFIRM SELECTOR" / "DRY-RUN" are best-effort and get corrected    ║
║  by the user's manual validation — the flow around them is real and tested ║
║  with a mocked bridge.                                                      ║
╚══════════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable
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


# --- Delta-specific constants — from live reconnaissance ----------------
# CONFIRMED = verified against the live site. CONFIRM SELECTOR / DRY-RUN =
# best-effort, to be corrected by the user's manual validation. Correcting
# these is the whole of the remaining fix-up; the logic is platform-agnostic.

DELTA_URLS = {
    # CONFIRMED — supplier login starts from the homepage; Delta redirects an
    # unauthenticated user to its login page automatically when they hit a
    # supplier page (so login_url points at the Response Manager below).
    "login": "https://www.delta-esourcing.com/",
    # CONFIRMED — the Response Manager. Logged-in suppliers enter a tender
    # access code here; unauthenticated users are bounced to login.
    "response_manager": (
        "https://www.delta-esourcing.com/delta/suppliers/select/addToList.html"
    ),
    # CONFIRMED — legacy direct-respond URL for numeric notice ids (the third
    # URL pattern); %s is the noticeId. No access-code box is involved.
    "legacy_respond": (
        "https://www.delta-esourcing.com/delta/respondToList.html?noticeId=%s"
    ),
}

DELTA_SELECTORS = {
    # CONFIRM SELECTOR (placeholder #1) — the "Access Code" text input.
    "access_code_input": (
        "input#accessCode, input[name='accessCode'], input[name*='access']"
    ),
    # CONFIRM SELECTOR (placeholder #2) — the "Submit" button next to it.
    "submit_button": (
        "button[type='submit'], input[type='submit'], button:has-text('Submit')"
    ),
    # CONFIRMED — the literal error banner shown for a closed opportunity.
    # Matched as plain text against the rendered page (not a CSS selector).
    "not_open_error_text": "This opportunity is not currently open.",
    # CONFIRM SELECTOR — a stable left-menu item that only renders for an
    # authenticated supplier (Profile Manager / Response Manager / etc.).
    "logged_in_marker": (
        "a:has-text('Response Manager'), a:has-text('Profile Manager'), "
        "#supplierMenu, nav a:has-text('Resources')"
    ),
    # CONFIRM SELECTOR (placeholder #3) — the "Responses" table that lists the
    # opportunities added via access code.
    "responses_table": (
        "table#responses, table.responses, table:has-text('Responses')"
    ),
    # DRY-RUN (placeholder #3, the one screen not yet seen) — documents area.
    "documents_area": (
        "#documents, .tender-documents, section:has-text('Documents')"
    ),
    # DRY-RUN — individual document download anchors.
    "document_links": "a[href*='download'], a[href$='.pdf'], a[href$='.docx']",
    # DRY-RUN — the Express/Register Interest control that signals intent to
    # bid. Never auto-clicked: it goes through the needs_user_confirmation pause.
    "express_interest_button": (
        "a:has-text('Express Interest'), button:has-text('Express Interest'), "
        "a:has-text('Register Interest'), button:has-text('Register Interest')"
    ),
}

# href pattern used with the bridge's find-links to enumerate document URLs.
# DRY-RUN — confirm once the documents screen is seen.
DELTA_DOCUMENT_HREF_PATTERN = r"(download|\.pdf|\.docx|\.doc|\.xls|\.xlsx|\.zip)"

# Login-success URL pattern for wait-for-login: after the human logs in (and
# clears Microsoft Authenticator) Delta lands them in the supplier area. The
# orchestrator also falls back to is_authenticated(), so this is a hint.
DELTA_LOGIN_SUCCESS_PATTERN = r"delta-esourcing\.com/delta/suppliers/"

DELTA_DOMAIN_RE = re.compile(r"(^|\.)delta-esourcing\.com$", re.IGNORECASE)

# --- access-code extraction --------------------------------------------
# The access code comes from the tender notice we already ingest from FTS/CF.
# Three URL patterns appear in Delta notices; each ends in the code (or, for
# the legacy form, a numeric noticeId). We extract from the tender's own data —
# never by searching Delta (it has no supplier-side search).

# https://www.delta-esourcing.com/respond/286EVX23TV
_RESPOND_RE = re.compile(
    r"delta-esourcing\.com/respond/([A-Za-z0-9]{4,})(?![A-Za-z0-9])",
    re.IGNORECASE,
)
# https://www.delta-esourcing.com/tenders/{description}/W5C25992M5
_TENDERS_RE = re.compile(
    r"delta-esourcing\.com/tenders/[^\s\"'<>]+/([A-Za-z0-9]{4,})(?![A-Za-z0-9])",
    re.IGNORECASE,
)
# https://www.delta-esourcing.com/delta/respondToList.html?noticeId=1032668140
_LEGACY_RE = re.compile(
    r"respondToList\.html\?[^\s\"'<>]*?\bnoticeId=(\d+)",
    re.IGNORECASE,
)

_CODE_PATTERNS = (_RESPOND_RE, _TENDERS_RE, _LEGACY_RE)


def extract_access_code(*sources: str | None) -> str | None:
    """Return the Delta access code (or legacy numeric noticeId) found in any of
    the given strings — typically the tender's source_url, description, and
    candidate document URLs. None if no Delta URL pattern is present.

    Sources are scanned in order; within each, the patterns are tried
    respond → tenders → legacy, so the first match wins.
    """
    for src in sources:
        if not src:
            continue
        for rx in _CODE_PATTERNS:
            m = rx.search(src)
            if m:
                return m.group(1)
    return None


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
        # Point the visible window at the Response Manager; Delta redirects an
        # unauthenticated user to its real login page automatically.
        return DELTA_URLS["response_manager"]

    def login_success_pattern(self) -> str:
        return DELTA_LOGIN_SUCCESS_PATTERN

    def _code_sources(self, ctx: PortalContext) -> Iterable[str | None]:
        return (ctx.source_url, ctx.description, *ctx.candidate_urls, ctx.tender_ref)

    async def is_authenticated(self, ctx: PortalContext) -> bool:
        """Navigate to the Response Manager; authenticated if we're not bounced
        to login and the logged-in supplier menu is present. Logs whether the
        session was reused or a fresh login is required."""
        bridge, slug = ctx.bridge, ctx.platform_slug
        if bridge is None or slug is None:
            return False
        try:
            await bridge.navigate(slug, DELTA_URLS["response_manager"])
            status = await bridge.session_status(slug)
        except Exception as exc:  # noqa: BLE001
            logger.info("delta.fresh_login_required", reason="navigate_failed",
                        error=str(exc))
            return False
        current = (status.get("current_url") or "").lower()
        if "login" in current:
            logger.info("delta.fresh_login_required", reason="redirected_to_login")
            return False
        try:
            marker = await bridge.element_exists(
                slug, DELTA_SELECTORS["logged_in_marker"]
            )
        except Exception:  # noqa: BLE001
            marker = False
        if marker:
            logger.info("delta.session_reused", slug=slug)
            return True
        logger.info("delta.fresh_login_required", reason="no_logged_in_marker")
        return False

    async def authenticate(
        self, ctx: PortalContext, creds: Credentials | None
    ) -> AuthResult:
        # Login is performed by the human in the visible window (password +
        # Microsoft Authenticator); the orchestrator only calls this once
        # wait-for-login has confirmed it. The adapter never submits credentials.
        return AuthResult(status=AuthStatus.success, detail="human login (no creds)")

    # --- locate / interest / download ----------------------------------

    async def locate_tender(
        self, ctx: PortalContext, tender_ref: str
    ) -> LocateResult:
        """Submit the tender's access code on the Response Manager. There is NO
        search on Delta — the code comes from the notice we already ingest."""
        bridge, slug = ctx.bridge, ctx.platform_slug
        if bridge is None or slug is None:
            return LocateResult(status=LocateStatus.error, detail="no bridge session")

        code = extract_access_code(*self._code_sources(ctx))
        if not code:
            return LocateResult(
                status=LocateStatus.not_found,
                detail="No Delta access code found in the tender notice.",
            )

        try:
            if code.isdigit():
                # Legacy numeric notice: direct respond URL, no access-code box.
                url = DELTA_URLS["legacy_respond"] % code
                logger.info("delta.locate_legacy", slug=slug, notice_id=code)
                await bridge.navigate(slug, url)
            else:
                logger.info("delta.locate_access_code", slug=slug, code=code)
                await bridge.navigate(slug, DELTA_URLS["response_manager"])
                await bridge.fill(
                    slug, DELTA_SELECTORS["access_code_input"], code
                )
                await bridge.click(slug, DELTA_SELECTORS["submit_button"])
            text = await bridge.page_text(slug)
        except Exception as exc:  # noqa: BLE001
            return LocateResult(status=LocateStatus.error, detail=str(exc))

        # Closed tender — a common, expected state. Handle gracefully.
        if DELTA_SELECTORS["not_open_error_text"].lower() in (text or "").lower():
            logger.info("delta.tender_closed", slug=slug, code=code)
            return LocateResult(
                status=LocateStatus.not_found,
                detail='Tender is closed on Delta ("not currently open").',
            )

        try:
            docs = await bridge.element_exists(
                slug, DELTA_SELECTORS["documents_area"]
            )
            interest = await bridge.element_exists(
                slug, DELTA_SELECTORS["express_interest_button"]
            )
            in_responses = await bridge.element_exists(
                slug, DELTA_SELECTORS["responses_table"]
            )
        except Exception as exc:  # noqa: BLE001
            return LocateResult(status=LocateStatus.error, detail=str(exc))

        # Documents gated behind Express Interest → pause for the user. The
        # orchestrator must NOT auto-click it (signals intent to bid).
        if interest and not docs:
            return LocateResult(
                status=LocateStatus.requires_interest_first,
                tender_url=DELTA_URLS["response_manager"],
                detail=(
                    "Delta requires you to Express Interest before documents are "
                    "released. This signals intent to bid. Confirm to proceed."
                ),
            )
        # Tender is in the Responses table (or its documents are already shown).
        if in_responses or docs:
            return LocateResult(
                status=LocateStatus.found, tender_url=DELTA_URLS["response_manager"]
            )
        return LocateResult(
            status=LocateStatus.not_found,
            detail="Access code submitted but the tender did not appear in Responses.",
        )

    async def register_interest(self, ctx: PortalContext) -> RegisterResult:
        """Click Express Interest. The orchestrator only calls this AFTER the
        user explicitly confirms via the needs_user_confirmation pause — never
        automatically."""
        bridge, slug = ctx.bridge, ctx.platform_slug
        if bridge is None or slug is None:
            return RegisterResult(status=RegisterStatus.error, detail="no bridge")
        try:
            await bridge.click(slug, DELTA_SELECTORS["express_interest_button"])
        except Exception as exc:  # noqa: BLE001
            return RegisterResult(status=RegisterStatus.error, detail=str(exc))
        logger.info("delta.express_interest_clicked", slug=slug)
        return RegisterResult(status=RegisterStatus.success)

    async def download_documents(
        self, ctx: PortalContext, dest_dir: str
    ) -> DownloadResult:
        """Enumerate document links in the opened tender and download each via
        the authenticated bridge session, applying the chunk-3 caps + sha256
        dedup. (The exact documents screen is a DRY-RUN item; for now we
        enumerate links on the current page.)"""
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
        seen_sha: set[str] = set()
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
            elif df.sha256 in seen_sha:
                # sha256 dedup: skip a duplicate of a file already taken.
                logger.info("delta.duplicate_skipped", url=url, sha256=df.sha256)
            else:
                seen_sha.add(df.sha256)
                files.append(df)

        if files and missing:
            status = DownloadStatus.partial
        elif files:
            status = DownloadStatus.complete
        else:
            status = (
                DownloadStatus.partial if missing else DownloadStatus.nothing_available
            )
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
