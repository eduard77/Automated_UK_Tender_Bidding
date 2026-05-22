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
    # BEST-EFFORT — Delta supplier logout, used to release the single concurrent
    # session so the human isn't locked out. Investigate/confirm the exact URL
    # against the live logged-in menu; logout() also falls back to clicking the
    # menu "Logout" control if this navigation doesn't end the session.
    "logout": "https://www.delta-esourcing.com/delta/logout.html",
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
    # CONFIRMED — the Response Manager "Responses" table lists the supplier's
    # registered opportunities; each opportunity name is a link whose href
    # already carries the Stage One ids (see STAGE_ONE_LINK_REGEX).
    "responses_table": (
        "table:has-text('Opportunity'), table#responses, table.responses"
    ),
    "responses_opportunity_link": "a[href*='suppRespStatus.html']",
    # CONFIRMED — the opportunity link href holds BOTH Stage One ids, so an
    # already-registered tender needs no Register-Interest click or redirect.
    "STAGE_ONE_LINK_REGEX": r"suppRespStatus\.html\?id=(\d+)&listId=(\d+)",
    # CONFIRMED — Delta blocks a second concurrent login with this banner.
    "concurrent_login_text": "Concurrent Logins Are Not Enabled",
    # BEST-EFFORT — the logged-in menu "Logout" control, used as a fallback when
    # the logout URL doesn't end the session.
    "logout_control": (
        "a:has-text('Logout'), a:has-text('Log out'), a:has-text('Sign out'), "
        "a:has-text('Log Off')"
    ),
}

_DOWNLOAD_LINK_RE = re.compile(DELTA_SELECTORS["download_link_regex"], re.IGNORECASE)
_STAGE_ONE_LINK_RE = re.compile(DELTA_SELECTORS["STAGE_ONE_LINK_REGEX"], re.IGNORECASE)

# Login-success URL pattern for wait-for-login: after the human logs in (and
# clears Microsoft Authenticator) Delta lands them in the supplier area. The
# orchestrator also falls back to is_authenticated(), so this is a hint.
DELTA_LOGIN_SUCCESS_PATTERN = r"delta-esourcing\.com/delta/suppliers/"

DELTA_DOMAIN_RE = re.compile(r"(^|\.)delta-esourcing\.com$", re.IGNORECASE)

REGISTER_INTEREST_DETAIL = (
    "Delta requires you to Register Interest in this tender before documents are "
    "released. This tells the buyer you intend to bid. Confirm to proceed."
)

ALREADY_REGISTERED_DETAIL = (
    "Your organisation has already registered interest in this tender on Delta. "
    "Confirm to pull its documents — no further Register Interest is needed."
)

# Delta enforces a single concurrent login per account. If a second login is
# active (e.g. the user is logged in elsewhere) Delta blocks us with this banner.
CONCURRENT_LOGIN_DETAIL = (
    "Delta session conflict — another Delta login is active for this account. "
    "End your other Delta session (Delta's \"End Session\" email), then retry."
)

_CONCURRENT_LOGIN_MARKERS = (
    DELTA_SELECTORS["concurrent_login_text"].lower(),
    "currently in use",
)


def _has_concurrent_login(text: str | None) -> bool:
    """True if the page shows Delta's single-session block."""
    low = (text or "").lower()
    return any(marker in low for marker in _CONCURRENT_LOGIN_MARKERS)

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


# --- Responses-table parsing (the already-registered path) --------------
# The Response Manager lists the supplier's registered opportunities. Each
# opportunity name is a link whose href already carries both Stage One ids — so
# for an already-registered tender we read the ids straight off the link, with
# no Register-Interest click and no redirect needed.


@dataclass
class _RespRow:
    resp_id: str
    list_id: str
    title: str | None


_RESP_ROW_ANCHOR_RE = re.compile(
    r"(?is)<a\b[^>]*?href=[\"']"
    r"([^\"']*suppRespStatus\.html\?id=\d+&listId=\d+[^\"']*)[\"']"
    r"[^>]*>(.*?)</a>"
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Below this Jaccard score a Responses row is not considered a match (only used
# to disambiguate when the table has more than one row).
MATCH_MIN_SCORE = 0.15


def _parse_responses_rows(html: str) -> list[_RespRow]:
    """Enumerate the supplier's registered opportunities from the Responses
    table HTML by the opportunity-link pattern (which carries id/listId). One
    row per distinct (id, listId); title is the link text."""
    rows: list[_RespRow] = []
    seen: set[tuple[str, str]] = set()
    # Real Delta markup encodes the ampersand as &amp;; decode so the CONFIRMED
    # link regex (which uses a literal &) matches both live HTML and fixtures.
    decoded = (html or "").replace("&amp;", "&")
    for m in _RESP_ROW_ANCHOR_RE.finditer(decoded):
        href, inner = m.group(1), m.group(2)
        ids = _STAGE_ONE_LINK_RE.search(href)
        if not ids:
            continue
        rid, lid = ids.group(1), ids.group(2)
        if (rid, lid) in seen:
            continue
        seen.add((rid, lid))
        title = re.sub(r"\s+", " ", _TAG_RE.sub(" ", inner)).strip() or None
        rows.append(_RespRow(rid, lid, title))
    return rows


def _title_tokens(value: str | None) -> set[str]:
    """Significant (>=3 char) lowercased word tokens of a title."""
    return {tok for tok in _TOKEN_RE.findall((value or "").lower()) if len(tok) >= 3}


def _title_score(a: str | None, b: str | None) -> float:
    """Jaccard overlap of two titles' significant tokens (0.0–1.0)."""
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return 0.0
    union = len(ta | tb)
    return len(ta & tb) / union if union else 0.0


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

    def _match_responses_row(
        self, html: str, ctx: PortalContext
    ) -> _RespRow | None:
        """Find THIS tender in the Responses table. The table lists the
        supplier's own registered opportunities and is typically short, so we
        match by title (Jaccard token overlap against the tender title). A
        single row is unambiguous; with several rows we pick the best match
        above MATCH_MIN_SCORE. Logs what matched."""
        rows = _parse_responses_rows(html)
        if not rows:
            return None
        title = ctx.title
        if not title:
            # No tender title to compare against: only a single row is safe.
            if len(rows) == 1:
                logger.info(
                    "delta.responses_match", reason="single_row_no_title",
                    resp_id=rows[0].resp_id, list_id=rows[0].list_id,
                    row_title=rows[0].title,
                )
                return rows[0]
            logger.info(
                "delta.responses_no_match", reason="no_title_multiple_rows",
                rows=len(rows),
            )
            return None
        scored = sorted(
            ((row, _title_score(title, row.title)) for row in rows),
            key=lambda pair: pair[1],
            reverse=True,
        )
        best, score = scored[0]
        # A lone row needs only some overlap; multiple rows must clear the
        # threshold so we never grab a different registered opportunity.
        threshold = 0.0 if len(rows) == 1 else MATCH_MIN_SCORE
        if score > threshold:
            logger.info(
                "delta.responses_match", resp_id=best.resp_id,
                list_id=best.list_id, row_title=best.title,
                score=round(score, 3), rows=len(rows),
            )
            return best
        logger.info(
            "delta.responses_no_match", best_title=best.title,
            best_score=round(score, 3), rows=len(rows),
        )
        return None

    async def locate_tender(
        self, ctx: PortalContext, tender_ref: str
    ) -> LocateResult:
        """Locate the tender, ALWAYS checking the Responses table first.

        Delta does not redirect an already-registered tender from its notice
        page to Stage One — instead the Response Manager's "Responses" table
        lists it, and the opportunity link already carries both Stage One ids.
        So we read the ids straight off that link (no Register Interest, no
        redirect). Only when the tender is NOT in the table do we open the
        notice page and gate on REGISTER INTEREST.

        Either reachable case returns requires_interest_first so the orchestrator
        pauses for confirmation before the FIRST fetch; the resume path then
        skips Register Interest when the ids are already captured."""
        bridge, slug = ctx.bridge, ctx.platform_slug
        if bridge is None or slug is None:
            return LocateResult(status=LocateStatus.error, detail="no bridge session")

        code = extract_access_code(*self._code_sources(ctx))
        if not code:
            return LocateResult(
                status=LocateStatus.not_found,
                detail="No Delta access code found in the tender notice.",
            )

        # 1. Always check the Responses table first (already-registered path).
        try:
            await bridge.navigate(slug, DELTA_URLS["response_manager"])
            rm_text = await bridge.page_text(slug)
        except Exception as exc:  # noqa: BLE001
            return LocateResult(status=LocateStatus.error, detail=str(exc))
        if _has_concurrent_login(rm_text):
            logger.info("delta.concurrent_login", slug=slug, where="response_manager")
            return LocateResult(status=LocateStatus.error, detail=CONCURRENT_LOGIN_DETAIL)
        try:
            rm_html = await bridge.page_html(slug)
        except Exception:  # noqa: BLE001
            rm_html = ""
        match = self._match_responses_row(rm_html, ctx)
        if match is not None:
            self._resp_id, self._list_id = match.resp_id, match.list_id
            logger.info(
                "delta.already_registered", slug=slug, code=code,
                resp_id=match.resp_id, list_id=match.list_id,
            )
            return LocateResult(
                status=LocateStatus.requires_interest_first,
                tender_url=DELTA_URLS["stage_one"] % (match.resp_id, match.list_id),
                detail=ALREADY_REGISTERED_DETAIL,
            )

        # 2. Not registered yet → open the notice page; gate on REGISTER INTEREST.
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

        if _has_concurrent_login(text):
            logger.info("delta.concurrent_login", slug=slug, where="notice")
            return LocateResult(status=LocateStatus.error, detail=CONCURRENT_LOGIN_DETAIL)

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

        # No Register Interest button and not in the Responses table. Some
        # already-registered notices still expose a Stage One link directly —
        # capture its ids so the resume path skips Register Interest.
        try:
            for u in await bridge.find_links(slug, r"suppRespStatus\.html"):
                rid, lid = _parse_stage_one_ids(u)
                if rid and lid:
                    self._resp_id, self._list_id = rid, lid
                    logger.info("delta.stage_one_ids_on_notice", resp_id=rid, list_id=lid)
                    return LocateResult(
                        status=LocateStatus.requires_interest_first,
                        tender_url=DELTA_URLS["stage_one"] % (rid, lid),
                        detail=ALREADY_REGISTERED_DETAIL,
                    )
        except Exception:  # noqa: BLE001
            pass
        if DELTA_SELECTORS["open_marker_text"].lower() in low:
            return LocateResult(
                status=LocateStatus.requires_interest_first,
                tender_url=url,
                detail=REGISTER_INTEREST_DETAIL,
            )
        return LocateResult(
            status=LocateStatus.not_found,
            detail="Notice loaded but no REGISTER INTEREST button or open marker found.",
        )

    async def register_interest(self, ctx: PortalContext) -> RegisterResult:
        """Register interest, but ONLY if the tender isn't already registered.

        The orchestrator calls this AFTER the user confirms via the
        needs_user_confirmation pause — never automatically. If locate already
        captured the Stage One ids (the tender is in the Responses table) we
        must NOT re-register: return already_registered without clicking.
        Otherwise click REGISTER INTEREST on the notice page and capture the
        Stage One ids from the resulting Stage One page (or the Responses table
        Delta adds the tender to)."""
        bridge, slug = ctx.bridge, ctx.platform_slug
        if bridge is None or slug is None:
            return RegisterResult(status=RegisterStatus.error, detail="no bridge")

        # Already registered (ids captured during locate): never click again.
        if self._resp_id and self._list_id:
            logger.info(
                "delta.register_interest_skipped", slug=slug,
                reason="already_registered",
                resp_id=self._resp_id, list_id=self._list_id,
            )
            return RegisterResult(status=RegisterStatus.already_registered)

        # Not registered → open the notice page and click REGISTER INTEREST.
        code = extract_access_code(*self._code_sources(ctx))
        if code:
            url = (
                DELTA_URLS["legacy_respond"] % code
                if code.isdigit()
                else DELTA_URLS["respond_landing"] % code
            )
            try:
                await bridge.navigate(slug, url)
            except Exception as exc:  # noqa: BLE001
                return RegisterResult(status=RegisterStatus.error, detail=str(exc))
        try:
            await bridge.click(slug, DELTA_SELECTORS["register_interest_button"])
        except Exception as exc:  # noqa: BLE001
            return RegisterResult(status=RegisterStatus.error, detail=str(exc))
        logger.info("delta.register_interest_clicked", slug=slug)

        # Capture the Stage One ids: first from the resulting URL (Delta usually
        # redirects to Stage One), else from the Responses table it now lists.
        rid, lid = None, None
        try:
            status = await bridge.session_status(slug)
            rid, lid = _parse_stage_one_ids(status.get("current_url"))
        except Exception:  # noqa: BLE001
            pass
        if not (rid and lid):
            try:
                await bridge.navigate(slug, DELTA_URLS["response_manager"])
                rm_html = await bridge.page_html(slug)
                match = self._match_responses_row(rm_html, ctx)
                if match is not None:
                    rid, lid = match.resp_id, match.list_id
            except Exception:  # noqa: BLE001
                pass
        if rid and lid:
            self._resp_id, self._list_id = rid, lid
            logger.info("delta.stage_one_ids", resp_id=rid, list_id=lid)
            return RegisterResult(status=RegisterStatus.success)
        return RegisterResult(
            status=RegisterStatus.error,
            detail=(
                "Registered interest but could not locate Stage One page — "
                "Delta may require a moment; retry."
            ),
        )

    async def session_conflict(self, ctx: PortalContext) -> str | None:
        """Probe an authenticated Delta page for the single-concurrent-login
        block. Returns an actionable message if Delta is blocking us because a
        second login is active, else None. Called once at fetch start so the
        orchestrator fails fast instead of looping on waiting_for_login."""
        bridge, slug = ctx.bridge, ctx.platform_slug
        if bridge is None or slug is None:
            return None
        try:
            await bridge.navigate(slug, DELTA_URLS["response_manager"])
            text = await bridge.page_text(slug)
        except Exception:  # noqa: BLE001
            return None
        if _has_concurrent_login(text):
            logger.info("delta.concurrent_login", slug=slug, where="session_conflict")
            return CONCURRENT_LOGIN_DETAIL
        return None

    async def logout(self, ctx: PortalContext) -> bool:
        """Best-effort logout to release Delta's single concurrent session so
        the real user isn't locked out. Tries the logout URL first, then the
        logged-in menu "Logout" control. Non-fatal — if it can't end the
        session, the user can recover via Delta's "End Session" email."""
        bridge, slug = ctx.bridge, ctx.platform_slug
        if bridge is None or slug is None:
            return False
        ok = False
        try:
            await bridge.navigate(slug, DELTA_URLS["logout"])
            ok = True
        except Exception as exc:  # noqa: BLE001
            logger.info("delta.logout_url_failed", error=str(exc))
        if not ok:
            try:
                if await bridge.element_exists(slug, DELTA_SELECTORS["logout_control"]):
                    await bridge.click(slug, DELTA_SELECTORS["logout_control"])
                    ok = True
            except Exception as exc:  # noqa: BLE001
                logger.info("delta.logout_control_failed", error=str(exc))
        logger.info("delta.logout", slug=slug, ok=ok)
        return ok

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
