"""Delta eSourcing adapter (platform_slug='delta_esourcing').

Login model: a human logs in to the real Delta site in the visible browser
window the bridge opens — password AND Microsoft Authenticator (app-based 2FA),
entirely by hand. This adapter NEVER submits credentials or 2FA codes and never
sees the password. Because of the authenticator, the persistent bridge session
is reused across fetches so the human re-authenticates as rarely as possible.

╔══════════════════════════════════════════════════════════════════════════╗
║  REAL-FLOW NOTE (corrected from chunk 4b after live recon + screenshots).  ║
║  1. The access code rides in the URL. /respond/{CODE} redirects to         ║
║     respondToList.html?accessCode={CODE}, which loads the NOTICE page       ║
║     directly — there is NO access-code box to fill.                         ║
║  2. The notice gates documents behind a "REGISTER INTEREST" button. That    ║
║     click signals intent to bid, so the orchestrator pauses                 ║
║     (needs_user_confirmation) and only clicks it on explicit user confirm.  ║
║  3. After Register Interest + login, Delta lands on the Stage One Overview  ║
║     (suppRespStatus.html?id={RESP_ID}&listId={LIST_ID}); those two ids      ║
║     build the document download URLs.                                       ║
║  4. Each document is a direct GET link                                      ║
║     (downloadDocument.html?respId=&supplierListId=&docId=) — we GET each    ║
║     through the authenticated session; we do NOT click per-row menus.       ║
║                                                                            ║
║  CONFIRMED constants are validated against the live site. Selectors marked  ║
║  "CONFIRM SELECTOR" / "DRY-RUN" are best-effort; the flow around them is    ║
║  real and tested with a mocked bridge.                                      ║
╚══════════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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
# CONFIRMED = verified against the live site (with a real account + screenshots).
# CONFIRM SELECTOR / DRY-RUN = best-effort, corrected by the user's manual
# validation. Correcting these is the whole of the remaining fix-up; the logic
# around them is platform-agnostic.

DELTA_URLS = {
    # CONFIRMED — supplier login starts from the homepage; Delta redirects an
    # unauthenticated user to login when they hit a supplier page.
    "login": "https://www.delta-esourcing.com/",
    # CONFIRMED — Response Manager, used only as an authenticated-session probe.
    "response_manager": (
        "https://www.delta-esourcing.com/delta/suppliers/select/addToList.html"
    ),
    # CONFIRMED — the notice page. /respond/{CODE} redirects here; loading it
    # directly with the access code shows the tender notice (no form to fill).
    "respond_landing": (
        "https://www.delta-esourcing.com/delta/respondToList.html?accessCode=%s"
    ),
    # CONFIRMED — legacy direct-respond URL for numeric notice ids.
    "legacy_respond": (
        "https://www.delta-esourcing.com/delta/respondToList.html?noticeId=%s"
    ),
    # CONFIRMED — Stage One: Overview, reached after Register Interest + login.
    # %s, %s = respId (id), listId. The documents table lives here.
    "stage_one": (
        "https://www.delta-esourcing.com/delta/suppliers/select/"
        "suppRespStatus.html?id=%s&listId=%s"
    ),
    # CONFIRMED — direct document download. %s, %s, %s = respId, listId, docId.
    "document_download": (
        "https://www.delta-esourcing.com/delta/suppliers/response/overview/"
        "documents/downloadDocument.html?respId=%s&supplierListId=%s&docId=%s"
    ),
}

DELTA_SELECTORS = {
    # CONFIRMED — the notice page button label is exactly "REGISTER INTEREST".
    # Playwright :has-text is case-insensitive substring, so this matches it.
    "register_interest_button": (
        "a:has-text('Register Interest'), button:has-text('Register Interest'), "
        "input[type='submit'][value*='Register Interest' i]"
    ),
    "register_interest_button_text": "REGISTER INTEREST",
    # CONFIRMED — open/closed banner text on the notice page.
    "open_marker_text": "currently OPEN",
    "closed_marker_text": "not currently open",
    # CONFIRM SELECTOR — a stable left-menu item that only renders for an
    # authenticated supplier (Profile Manager / Response Manager / etc.).
    "logged_in_marker": (
        "a:has-text('Response Manager'), a:has-text('Profile Manager'), "
        "a:has-text('Select Accredit'), #supplierMenu, nav a:has-text('Resources')"
    ),
    # CONFIRM SELECTOR — the Stage One documents table (columns
    # Document Title / Size / File Type / Uploaded / Action).
    "documents_table": (
        "table:has-text('Document Title'), table#documents, table.documents"
    ),
    # CONFIRM SELECTOR — the "Display N items per page" dropdown.
    "page_size_select": (
        "select[name*='perPage' i], select[name*='pageSize' i], "
        "select[aria-label*='per page' i], select.page-size"
    ),
    # CONFIRMED — extract respId, supplierListId, docId from a download link.
    "download_link_regex": (
        r"downloadDocument\.html\?respId=(\d+)&supplierListId=(\d+)&docId=(\d+)"
    ),
}

_DOWNLOAD_LINK_RE = re.compile(DELTA_SELECTORS["download_link_regex"], re.IGNORECASE)

# Login-success URL pattern for wait-for-login: after the human logs in (and
# clears Microsoft Authenticator) Delta lands them in the supplier area. The
# orchestrator also falls back to is_authenticated(), so this is a hint.
DELTA_LOGIN_SUCCESS_PATTERN = r"delta-esourcing\.com/delta/suppliers/"

DELTA_DOMAIN_RE = re.compile(r"(^|\.)delta-esourcing\.com$", re.IGNORECASE)

REGISTER_INTEREST_DETAIL = (
    "Delta requires you to Register Interest in this tender before documents are "
    "released. This tells the buyer you intend to bid. Confirm to proceed."
)

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


def _parse_stage_one_ids(url: str | None) -> tuple[str | None, str | None]:
    """Pull (respId, listId) from a Stage One URL's `id`/`listId` query params."""
    if not url:
        return None, None
    q = parse_qs(urlparse(url).query)
    return (
        (q.get("id") or [None])[0],
        (q.get("listId") or [None])[0],
    )


@dataclass
class _DocRow:
    resp_id: str
    list_id: str
    doc_id: str
    title: str | None


_TR_SPLIT_RE = re.compile(r"(?i)<tr\b")
_FIRST_CELL_RE = re.compile(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>")
_TAG_RE = re.compile(r"(?s)<[^>]+>")


def _first_cell_text(row_html: str) -> str | None:
    """Best-effort document title = text of the row's first cell (Document
    Title is column one). DRY-RUN: confirm the column order on the live site."""
    m = _FIRST_CELL_RE.search(row_html)
    if not m:
        return None
    text = _TAG_RE.sub(" ", m.group(1))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _parse_document_rows(html: str) -> list[_DocRow]:
    """Enumerate document rows from the Stage One documents table HTML by the
    download-link pattern (which carries respId/listId/docId). One row per
    distinct docId; title is best-effort from the first cell."""
    rows: list[_DocRow] = []
    seen: set[str] = set()
    for seg in _TR_SPLIT_RE.split(html or ""):
        m = _DOWNLOAD_LINK_RE.search(seg)
        if not m:
            continue
        resp_id, list_id, doc_id = m.group(1), m.group(2), m.group(3)
        if doc_id in seen:
            continue
        seen.add(doc_id)
        rows.append(_DocRow(resp_id, list_id, doc_id, _first_cell_text(seg)))
    return rows


MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_DOCS = 50


class DeltaEsourcingAdapter(PortalAdapter):
    platform_slug = "delta_esourcing"
    requires_browser = False  # uses the Windows bridge, not an in-process page
    requires_login = True

    # Stage One ids captured after Register Interest; reused by download step.
    _resp_id: str | None = None
    _list_id: str | None = None

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
        """Open the notice page directly via the access-code URL. There is NO
        search and NO access-code form on Delta — the code rides in the URL.
        Detect open/closed and the REGISTER INTEREST gate."""
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
                url = DELTA_URLS["legacy_respond"] % code
                logger.info("delta.locate_legacy", slug=slug, notice_id=code)
            else:
                url = DELTA_URLS["respond_landing"] % code
                logger.info("delta.locate_access_code", slug=slug, code=code)
            await bridge.navigate(slug, url)
            text = await bridge.page_text(slug)
        except Exception as exc:  # noqa: BLE001
            return LocateResult(status=LocateStatus.error, detail=str(exc))

        low = (text or "").lower()
        # Closed tender — a common, expected state. Handle gracefully. (Check
        # before the open marker: "not currently open" contains "currently open".)
        if DELTA_SELECTORS["closed_marker_text"].lower() in low:
            logger.info("delta.tender_closed", slug=slug, code=code)
            return LocateResult(
                status=LocateStatus.not_found,
                detail='Tender is closed on Delta ("not currently open").',
            )

        # REGISTER INTEREST gate → pause for explicit user confirmation. The
        # orchestrator must NOT auto-click it (it signals intent to bid).
        has_register = DELTA_SELECTORS["register_interest_button_text"].lower() in low
        if not has_register:
            try:
                has_register = await bridge.element_exists(
                    slug, DELTA_SELECTORS["register_interest_button"]
                )
            except Exception:  # noqa: BLE001
                has_register = False
        if has_register:
            return LocateResult(
                status=LocateStatus.requires_interest_first,
                tender_url=url,
                detail=REGISTER_INTEREST_DETAIL,
            )

        # No Register Interest button: either already registered (documents
        # reachable) or simply open. An already-registered notice exposes a
        # Stage One link — capture its ids so the download step can reach the
        # documents without re-registering.
        try:
            for u in await bridge.find_links(slug, r"suppRespStatus\.html"):
                rid, lid = _parse_stage_one_ids(u)
                if rid and lid:
                    self._resp_id, self._list_id = rid, lid
                    logger.info("delta.stage_one_ids", resp_id=rid, list_id=lid)
                    break
        except Exception:  # noqa: BLE001
            pass
        if (self._resp_id and self._list_id) or (
            DELTA_SELECTORS["open_marker_text"].lower() in low
        ):
            return LocateResult(status=LocateStatus.found, tender_url=url)
        return LocateResult(
            status=LocateStatus.not_found,
            detail="Notice loaded but no REGISTER INTEREST button or open marker found.",
        )

    async def register_interest(self, ctx: PortalContext) -> RegisterResult:
        """Click REGISTER INTEREST. The orchestrator only calls this AFTER the
        user explicitly confirms via the needs_user_confirmation pause — never
        automatically. Captures the Stage One respId/listId from the resulting
        URL for the download step."""
        bridge, slug = ctx.bridge, ctx.platform_slug
        if bridge is None or slug is None:
            return RegisterResult(status=RegisterStatus.error, detail="no bridge")
        try:
            await bridge.click(slug, DELTA_SELECTORS["register_interest_button"])
        except Exception as exc:  # noqa: BLE001
            return RegisterResult(status=RegisterStatus.error, detail=str(exc))
        # After the click Delta lands on Stage One: capture the two ids.
        try:
            status = await bridge.session_status(slug)
            rid, lid = _parse_stage_one_ids(status.get("current_url"))
            if rid and lid:
                self._resp_id, self._list_id = rid, lid
                logger.info("delta.stage_one_ids", resp_id=rid, list_id=lid)
        except Exception:  # noqa: BLE001
            pass
        logger.info("delta.register_interest_clicked", slug=slug)
        return RegisterResult(status=RegisterStatus.success)

    async def _resolve_stage_one(
        self, bridge, slug: str
    ) -> tuple[str | None, str | None]:
        """Find the Stage One respId/listId: from the ids captured at Register
        Interest, else the current URL, else a Stage One link on the page."""
        if self._resp_id and self._list_id:
            return self._resp_id, self._list_id
        try:
            status = await bridge.session_status(slug)
            rid, lid = _parse_stage_one_ids(status.get("current_url"))
            if rid and lid:
                return rid, lid
        except Exception:  # noqa: BLE001
            pass
        try:
            for u in await bridge.find_links(slug, r"suppRespStatus\.html"):
                rid, lid = _parse_stage_one_ids(u)
                if rid and lid:
                    return rid, lid
        except Exception:  # noqa: BLE001
            pass
        return None, None

    async def _maximise_page_size(self, bridge, slug: str) -> None:
        """Best-effort: set the documents page-size dropdown to its largest
        option so every row renders on one page. Non-fatal — if it fails we
        enumerate whatever rows are present. DRY-RUN: confirm the select markup."""
        try:
            if await bridge.element_exists(slug, DELTA_SELECTORS["page_size_select"]):
                await bridge.select_option(
                    slug, DELTA_SELECTORS["page_size_select"], index=-1
                )
        except Exception as exc:  # noqa: BLE001
            logger.info("delta.page_size_max_skipped", error=str(exc))

    async def download_documents(
        self, ctx: PortalContext, dest_dir: str
    ) -> DownloadResult:
        """Download every Stage One document via a direct GET to
        downloadDocument.html (using the captured respId/listId + each row's
        docId) through the authenticated session. We do NOT click per-row menus.
        Applies the chunk-3 caps (100MB/doc, 50 docs), sanitised filenames, and
        sha256 dedup."""
        bridge, slug = ctx.bridge, ctx.platform_slug
        if bridge is None or slug is None:
            return DownloadResult(
                status=DownloadStatus.error, detail="no bridge session"
            )

        resp_id, list_id = await self._resolve_stage_one(bridge, slug)
        if not (resp_id and list_id):
            return DownloadResult(
                status=DownloadStatus.error,
                detail="could not determine Delta Stage One response/list ids",
            )

        try:
            await bridge.navigate(slug, DELTA_URLS["stage_one"] % (resp_id, list_id))
        except Exception as exc:  # noqa: BLE001
            return DownloadResult(status=DownloadStatus.error, detail=str(exc))

        # Make sure all rows are on one page, then read the documents table.
        await self._maximise_page_size(bridge, slug)
        try:
            html = await bridge.page_html(slug)
        except Exception as exc:  # noqa: BLE001
            return DownloadResult(status=DownloadStatus.error, detail=str(exc))

        rows = _parse_document_rows(html)
        if not rows:
            return DownloadResult(
                status=DownloadStatus.nothing_available,
                detail="no documents found on Stage One",
            )

        files: list[DownloadedFile] = []
        seen_sha: set[str] = set()
        missing: list[str] = []
        for row in rows[:MAX_DOCS]:
            url = DELTA_URLS["document_download"] % (resp_id, list_id, row.doc_id)
            if not self.matches_url(url):
                missing.append(url)
                continue
            dest_name = _safe_name(row.title) if row.title else None
            try:
                bf = await bridge.download(slug, url, dest_name)
            except Exception as exc:  # noqa: BLE001
                logger.info("delta.download_failed", url=url, error=str(exc))
                missing.append(url)
                continue
            df = _ingest_bridge_file(bf, url, ctx.tender_id, preferred_name=row.title)
            if df is None:
                missing.append(url)
            elif df.sha256 in seen_sha:
                logger.info("delta.duplicate_skipped", url=url, sha256=df.sha256)
            else:
                seen_sha.add(df.sha256)
                files.append(df)
        # Anything beyond the cap is reported as missing.
        for row in rows[MAX_DOCS:]:
            missing.append(
                DELTA_URLS["document_download"] % (resp_id, list_id, row.doc_id)
            )

        if files and missing:
            status = DownloadStatus.partial
        elif files:
            status = DownloadStatus.complete
        else:
            status = (
                DownloadStatus.partial if missing else DownloadStatus.nothing_available
            )
        return DownloadResult(status=status, files=files, missing=missing)


def _ingest_bridge_file(
    bf, url: str, tender_id: int, preferred_name: str | None = None
) -> DownloadedFile | None:
    """Read a bridge-downloaded file from the shared volume, size-cap it,
    sha256 it, and copy it into the tender-documents storage layout. The
    on-disk record uses preferred_name (the document title) when provided."""
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
    if preferred_name:
        filename = _safe_name(preferred_name)
    else:
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
