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
║  4. Stage One documents have NO per-row download link in the DOM. Delta     ║
║     uses checkboxes + a documentIds cookie + JS; the file only downloads     ║
║     when a human opens a row's ⋮ Action menu and clicks "Download File".     ║
║     So we drive that click per row (bridge click-download-in-row) and        ║
║     capture each download — like a human — rather than GETting a URL.        ║
║                                                                            ║
║  CONFIRMED constants are validated against the live site. Selectors marked  ║
║  "CONFIRM SELECTOR" / "DRY-RUN" are best-effort; the flow around them is    ║
║  real and tested with a mocked bridge.                                      ║
╚══════════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

import contextlib
import hashlib
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
    # CONFIRMED (live DOM, tender 3169) — the Stage One documents table is
    # <table id="document" class="dataTable ..." role="grid"> inside
    # <div id="documentList">. Columns: Document Title / Size / File Type /
    # Uploaded / Action.
    "documents_table": "table#document",
    # CONFIRMED (live DOM) — the data rows are tr[role="row"] in the tbody
    # (classes alternate odd/even); the thead row is excluded. Used to wait for
    # the render and to target each row's Action menu by index.
    "document_rows": "table#document tbody tr[role='row']",
    # CONFIRMED (live DOM) — the ⋮ "three dots" Action control is a Font Awesome
    # <i> icon (NOT a button/link), inside <bip-actions-menu> in the row's last
    # (Action) cell. The bridge finds it WITHIN the row, so this is a per-row
    # selector; clicking it opens the row's menu.
    "row_action_menu": "i.bip-actions-menu-link.fa-ellipsis-v",
    # CONFIRMED (live DOM) — after the ⋮ click a popup renders at BODY level (in a
    # div.highslide-container) containing a "Download File" control. The bridge
    # matches it PAGE-WIDE by this visible text (get_by_text .last), not within
    # the row.
    "row_download_item_text": "Download File",
    # CONFIRMED (live DOM) — the documents table is a DataTables grid; its length
    # menu is a <select name="document_length"> in the .dataTables_length control
    # ("Display N items per page"). Set it to the largest option so all rows
    # render on one page. Patterns also cover generic per-page selects.
    "page_size_select": (
        "select[name$='_length'], .dataTables_length select, "
        "select[name*='perPage' i], select[name*='pageSize' i], "
        "select[aria-label*='per page' i], select.page-size"
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
    """One Stage One document row. Delta exposes no per-row download link, so a
    row is just its display metadata: the Title (column 1, used to name the file)
    and the File Type (column 3, an extension fallback). The file is fetched by
    clicking the row's ⋮ Action menu — see download_documents."""

    title: str | None
    file_type: str | None


_TR_SPLIT_RE = re.compile(r"(?i)<tr\b")
_TAG_RE = re.compile(r"(?s)<[^>]+>")
# Isolate the Stage One documents table (CONFIRMED id="document") so we parse
# its rows and never another table on the page.
_TABLE_DOC_RE = re.compile(
    r"(?is)<table[^>]*\bid=[\"']document[\"'][^>]*>(.*?)</table>"
)
_CELL_RE = re.compile(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>")


def _cell_texts(row_html: str) -> list[str]:
    """Whitespace-normalised text of each cell in a row, in column order."""
    out: list[str] = []
    for m in _CELL_RE.finditer(row_html):
        out.append(re.sub(r"\s+", " ", _TAG_RE.sub(" ", m.group(1))).strip())
    return out


def _parse_document_rows(html: str) -> list[_DocRow]:
    """Enumerate the Stage One document rows from the RENDERED table#document.

    One _DocRow per DATA row (a row with <td> cells; header rows with only <th>
    are skipped). Title is column 1 and File Type is column 3 — there is NO
    download link or docId to extract (Delta downloads via the row's ⋮ menu)."""
    m = _TABLE_DOC_RE.search(html or "")
    # If the id isn't found (markup drift), fall back to the whole document so a
    # partially-rendered table still yields rows rather than silently zero.
    inner = m.group(1) if m else (html or "")
    rows: list[_DocRow] = []
    for seg in _TR_SPLIT_RE.split(inner):
        if "<td" not in seg.lower():
            continue  # header row (only <th>) or pre-row preamble
        cells = _cell_texts(seg)
        if not cells:
            continue
        title = cells[0] or None
        file_type = cells[2] if len(cells) > 2 else None
        rows.append(_DocRow(title=title, file_type=file_type))
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

# --- rendered-DOM waits (bip-table is JS-rendered) ----------------------
# Delta's tables (<bip-table> web components) are populated CLIENT-SIDE after
# load, so the rows (and the opportunity links) are absent from the initial
# server HTML and present only in the RENDERED DOM. We read via the bridge's
# rendered-html endpoint, waiting for a marker that proves the rows rendered.
DELTA_RENDER_TIMEOUT_MS = 15000  # primary wait for the rows to render
DELTA_RENDER_FALLBACK_TIMEOUT_MS = 6000  # each secondary render-signal wait

# Stage One documents: the rows carry NO download link, so the surest proof they
# rendered is a data row in table#document (a selector wait); the fallbacks give
# the table more time (the "items per page" control / its text) before we re-read.
DELTA_DOC_RENDER_FALLBACK_TEXTS = ("items per page",)

# After maximising the DataTables page size, the grid re-renders client-side; we
# re-read the rendered DOM up to this many times, waiting for all "N items" rows
# to appear, before giving up and downloading whatever rendered.
DELTA_DOC_REREAD_ATTEMPTS = 5

# Responses table (already-registered match): the opportunity link carries both
# Stage One ids; wait for it, falling back to the "Opportunity" header.
DELTA_RESP_RENDER_SIGNAL = "suppRespStatus.html"
DELTA_RESP_RENDER_FALLBACK_TEXTS = ("Opportunity",)

# --- robust Responses-table read (chunk 4h: fixes the render race) ------
# The bip-table renders its container first, then injects the row links a moment
# later. A single short wait can return BEFORE the links exist, making an
# already-registered tender read as "not registered" (intermittent — same tender,
# same account, pure timing). So we wait on the ACTUAL opportunity-link element
# (a real Playwright wait, not a text-substring poll) with a generous budget,
# distinguish a genuinely-empty table from one that hasn't rendered, and retry
# once before concluding not-registered.
DELTA_RESPONSES_WAIT_MS = 22000  # generous wait for the opportunity-link rows
DELTA_RESPONSES_CONTAINER_WAIT_MS = 4000  # quick probe: did the table container render?
DELTA_AUTH_MARKER_WAIT_MS = 8000  # wait for the logged-in menu marker (session reuse)

# Positive "you have no responses" indicators — when present we trust the table
# is genuinely empty (not registered) and do NOT retry.
_RESPONSES_EMPTY_RE = re.compile(
    r"no responses|you have not responded|have not registered|no opportunities|"
    r"\b0\s+items\b|no records found",
    re.IGNORECASE,
)

# Delta shows a row count like "Displaying 1 - 22 of 22 items" near the table.
# We use it ONLY to log a parser-vs-display mismatch (never to fail a download).
# "of N items" is the total; the bare "N items" form excludes "N items per page"
# (the page-size control), which is not a row total.
_ITEMS_OF_RE = re.compile(r"\bof\s+(\d+)\s+items?\b", re.IGNORECASE)
_ITEMS_COUNT_RE = re.compile(r"\b(\d+)\s+items?\b(?!\s+per\b)", re.IGNORECASE)


def _parse_items_count(html: str) -> int | None:
    """Best-effort total-row count Delta displays near the documents table, or
    None if absent. Prefers the "of N items" total; otherwise the largest bare
    "N items" figure (ignoring "N items per page")."""
    text = html or ""
    m = _ITEMS_OF_RE.search(text)
    if m:
        return int(m.group(1))
    nums = [int(mm.group(1)) for mm in _ITEMS_COUNT_RE.finditer(text)]
    return max(nums) if nums else None


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
        to login and the logged-in supplier menu is present.

        The supplier menu is part of the JS-rendered chrome, so we WAIT for the
        marker in the rendered DOM (a real element wait) rather than probing the
        current DOM once — an already-authenticated session that renders the menu
        a beat late used to read as "fresh login required" and either re-prompt
        or hang waiting_for_login (chunk 4h). Logs reuse vs fresh-login."""
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
        # Wait for the logged-in marker to render (robust to a slow menu render).
        try:
            res = await bridge.rendered_html(
                slug,
                wait_for_selector=DELTA_SELECTORS["logged_in_marker"],
                timeout_ms=DELTA_AUTH_MARKER_WAIT_MS,
            )
            marker = res.wait_satisfied
            # A late client-side bounce to login still means not authenticated.
            if not marker and "login" in (res.current_url or "").lower():
                logger.info("delta.fresh_login_required", reason="redirected_to_login")
                return False
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

    async def _read_responses_robust(
        self, bridge, slug: str
    ) -> tuple[str, str]:
        """Read the Response Manager "Responses" table robustly (chunk 4h).

        Returns (html, outcome) where outcome is one of:
          - "rows":  the opportunity-link rows rendered (registered tenders present)
          - "empty": a positive "no responses" indicator is shown (genuinely empty)
          - "empty_container": the table rendered but with no rows (ambiguous)
          - "unrendered": the table did not render at all (transient render race)

        We WAIT on the actual opportunity-link element (a real Playwright wait,
        not a content-substring poll) with a generous budget, so a slow bip-table
        render does not read as empty. The caller retries on the ambiguous /
        unrendered outcomes before concluding not-registered."""
        html = ""
        # 1. Wait for the opportunity-link rows themselves (the surest signal).
        try:
            r = await bridge.rendered_html(
                slug,
                wait_for_selector=DELTA_SELECTORS["responses_opportunity_link"],
                timeout_ms=DELTA_RESPONSES_WAIT_MS,
            )
            html = r.html or ""
            if r.wait_satisfied:
                return html, "rows"
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "delta.rendered_html_failed", where="responses_rows", error=str(exc)
            )
        # 2. No rows. A positive empty indicator means genuinely not registered.
        if _RESPONSES_EMPTY_RE.search(html):
            return html, "empty"
        # 3. Did the table CONTAINER render (header present, just no rows)?
        try:
            c = await bridge.rendered_html(
                slug,
                wait_for_selector=DELTA_SELECTORS["responses_table"],
                timeout_ms=DELTA_RESPONSES_CONTAINER_WAIT_MS,
            )
            html = c.html or html
            if _RESPONSES_EMPTY_RE.search(html):
                return html, "empty"
            if c.wait_satisfied:
                return html, "empty_container"
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "delta.rendered_html_failed", where="responses_container",
                error=str(exc),
            )
        return html, "unrendered"

    async def _match_in_responses(
        self, bridge, slug: str, ctx: PortalContext
    ) -> tuple[_RespRow | None, str]:
        """Robustly find THIS tender in the Responses table, with a bounded retry.

        A registered tender (the proven 3169 case) must be detected reliably, so
        a transient empty/unrendered read is re-navigated and re-read once before
        we conclude not-registered. Returns (match_or_None, final_outcome)."""
        html, outcome = await self._read_responses_robust(bridge, slug)
        match = self._match_responses_row(html, ctx) if outcome == "rows" else None
        if match is None and outcome in ("empty_container", "unrendered"):
            logger.info("delta.responses_retry", slug=slug, outcome=outcome)
            with contextlib.suppress(Exception):
                await bridge.navigate(slug, DELTA_URLS["response_manager"])
            html, outcome = await self._read_responses_robust(bridge, slug)
            match = self._match_responses_row(html, ctx) if outcome == "rows" else None
        if match is None:
            logger.info(
                "delta.responses_empty", slug=slug, outcome=outcome,
                positive_empty=(outcome == "empty"),
                rows=len(_parse_responses_rows(html)),
            )
        return match, outcome

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
        # The Responses table is a JS-rendered bip-table: read the RENDERED DOM
        # robustly (wait for the real opportunity-link rows, distinguish empty
        # from not-yet-rendered, retry once) so an already-registered tender is
        # detected reliably rather than intermittently (chunk 4h, BUG 1).
        match, _ = await self._match_in_responses(bridge, slug, ctx)
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
                already_authorized=True,
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
                        already_authorized=True,
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
                # Rendered DOM, robust: the Responses bip-table populates
                # client-side; wait for the real rows (with a retry) so the
                # just-registered tender is found rather than missed by timing.
                match, _ = await self._match_in_responses(bridge, slug, ctx)
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

    async def _read_rendered_html(
        self,
        bridge,
        slug: str,
        *,
        primary_text: str | None = None,
        primary_selector: str | None = None,
        fallback_texts: Iterable[str] = (),
        fallback_selector: str | None = None,
    ) -> str:
        """Read Delta's RENDERED DOM for a JS-rendered table.

        Delta's bip-table rows (and the opportunity links) are injected
        client-side, so the initial server HTML has only the empty shell. We wait
        for the PRIMARY render signal — a marker text (`primary_text`, e.g.
        "suppRespStatus.html" for the Responses table) OR a selector
        (`primary_selector`, e.g. a data row of table#document) — that proves the
        rows rendered; if it doesn't appear we wait on each fallback signal and
        re-read; finally we return whatever rendered so the caller can log/handle
        an empty table explicitly rather than silently. Bridge errors degrade to
        "" (same as the old page_html path)."""
        attempts: list[tuple[str, str, int]] = []
        if primary_selector is not None:
            attempts.append(("selector", primary_selector, DELTA_RENDER_TIMEOUT_MS))
        if primary_text is not None:
            attempts.append(("text", primary_text, DELTA_RENDER_TIMEOUT_MS))
        attempts += [
            ("text", t, DELTA_RENDER_FALLBACK_TIMEOUT_MS) for t in fallback_texts
        ]
        if fallback_selector is not None:
            attempts.append(
                ("selector", fallback_selector, DELTA_RENDER_FALLBACK_TIMEOUT_MS)
            )

        best = ""
        for kind, value, timeout_ms in attempts:
            try:
                if kind == "selector":
                    res = await bridge.rendered_html(
                        slug, wait_for_selector=value, timeout_ms=timeout_ms
                    )
                else:
                    res = await bridge.rendered_html(
                        slug, wait_for_text=value, timeout_ms=timeout_ms
                    )
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "delta.rendered_html_failed", kind=kind, value=value,
                    error=str(exc),
                )
                continue
            html = res.html or ""
            if html:
                best = html
            # The primary signal proves the rows we care about have rendered.
            if primary_text is not None and primary_text in html:
                return html
            if (
                primary_selector is not None
                and kind == "selector"
                and value == primary_selector
                and res.wait_satisfied
            ):
                return html
        logger.info(
            "delta.render_signal_absent",
            text=primary_text, selector=primary_selector,
        )
        return best

    async def _maximise_page_size(self, bridge, slug: str) -> bool:
        """Best-effort: set the documents DataTables length menu to its largest
        option (e.g. 100) so every row renders on one page. Returns True if it
        set the control. Non-fatal — if the select isn't found we enumerate
        whatever rows are present (and the caller polls/falls back)."""
        selector = DELTA_SELECTORS["page_size_select"]
        try:
            if not await bridge.element_exists(slug, selector):
                logger.info("delta.page_size_max_skipped", reason="select_not_found")
                return False
            # index=-1 selects the last (largest) option of the length menu.
            await bridge.select_option(slug, selector, index=-1)
            logger.info("delta.page_size_maximised", selector=selector)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.info("delta.page_size_max_skipped", error=str(exc))
            return False

    async def _read_documents(
        self, bridge, slug: str
    ) -> tuple[str, list[_DocRow], int | None]:
        """Read the Stage One documents table, polling until all rows render.

        DataTables re-renders client-side after the page-size change, so the
        first read can catch only the initial page. We read once (waiting for a
        data row), parse the "N items" total, then — if fewer than N rows are
        present — re-read up to DELTA_DOC_REREAD_ATTEMPTS times, keeping the
        largest result and stopping as soon as the count reaches N or stops
        growing. Returns (best_html, parsed_rows, items_count)."""
        html = await self._read_rendered_html(
            bridge, slug,
            primary_selector=DELTA_SELECTORS["document_rows"],
            fallback_texts=DELTA_DOC_RENDER_FALLBACK_TEXTS,
            fallback_selector=DELTA_SELECTORS["page_size_select"],
        )
        rows = _parse_document_rows(html)
        items_count = _parse_items_count(html)
        if items_count is None or len(rows) >= items_count:
            return html, rows, items_count

        for _ in range(DELTA_DOC_REREAD_ATTEMPTS):
            try:
                res = await bridge.rendered_html(
                    slug,
                    wait_for_selector=DELTA_SELECTORS["document_rows"],
                    timeout_ms=DELTA_RENDER_FALLBACK_TIMEOUT_MS,
                )
            except Exception as exc:  # noqa: BLE001
                logger.info("delta.documents_reread_failed", error=str(exc))
                break
            reread_rows = _parse_document_rows(res.html or "")
            if len(reread_rows) <= len(rows):
                break  # stabilised — no further rows appeared
            html, rows = (res.html or html), reread_rows
            if len(rows) >= items_count:
                break
        return html, rows, items_count

    async def download_documents(
        self, ctx: PortalContext, dest_dir: str
    ) -> DownloadResult:
        """Download every Stage One document by CLICKING each row's ⋮ Action menu
        and its "Download File" item, capturing the file the browser downloads —
        like a human. Delta exposes NO per-row download link or docId in the DOM
        (it uses checkboxes + a documentIds cookie + JS), so a direct GET is
        impossible; only the menu click triggers the download.

        We enumerate the rendered table#document rows (title + file type + count),
        then drive a per-row click-download via the bridge. Applies the chunk-3
        caps (100MB/doc, 50 docs), sanitised filenames, and sha256 dedup. Status
        is partial when some rows fail; a CLEAR error (never a silent
        nothing_available) when rows were seen but NONE could be downloaded."""
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

        stage_one_url = DELTA_URLS["stage_one"] % (resp_id, list_id)
        try:
            await bridge.navigate(slug, stage_one_url)
        except Exception as exc:  # noqa: BLE001
            return DownloadResult(status=DownloadStatus.error, detail=str(exc))

        # Maximise the page size FIRST (so all rows render on one page), THEN
        # read the RENDERED DOM — Delta's DataTables rows are injected by JS, so
        # the initial page HTML has only the empty shell. The surest proof the
        # rows rendered is a data row of table#document (a selector wait); after
        # maximising we poll until all "N items" rows appear.
        # Order matters: maximise → wait for render → read (with poll).
        await self._maximise_page_size(bridge, slug)
        html, rows, items_count = await self._read_documents(bridge, slug)
        if not rows:
            # ZERO rows seen → genuinely nothing to download (or the table never
            # rendered). Clear detail, not a silent empty result.
            logger.info(
                "delta.documents_none", resp_id=resp_id, list_id=list_id,
                items=items_count,
            )
            return DownloadResult(
                status=DownloadStatus.nothing_available,
                detail=(
                    "Stage One documents table did not render any document rows — "
                    "Delta may have changed its page structure."
                ),
            )

        # Visibility: the rows the parser saw vs the "N items" total Delta shows.
        render_incomplete = items_count is not None and len(rows) < items_count
        logger.info(
            "delta.documents_enumerated", rows=len(rows), items=items_count,
            resp_id=resp_id, list_id=list_id,
        )
        if render_incomplete:
            logger.info(
                "delta.documents_partial", rows_found=len(rows),
                items_displayed=items_count,
            )

        files: list[DownloadedFile] = []
        seen_sha: set[str] = set()
        missing: list[str] = []
        for i, row in enumerate(rows[:MAX_DOCS]):
            label = row.title or f"row {i}"
            try:
                rd = await bridge.click_download_in_row(
                    slug,
                    table_selector=DELTA_SELECTORS["documents_table"],
                    row_index=i,
                    menu_trigger_selector=DELTA_SELECTORS["row_action_menu"],
                    download_item_text=DELTA_SELECTORS["row_download_item_text"],
                )
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "delta.download_click_failed", row=i, title=row.title,
                    error=str(exc),
                )
                missing.append(label)
                continue
            df = _ingest_bridge_file(
                rd, ctx.tender_id, title=row.title, file_type=row.file_type,
                source_url=stage_one_url,
            )
            if df is None:
                logger.info(
                    "delta.download_click_failed", row=i, title=row.title,
                    error="ingest_failed",
                )
                missing.append(label)
            elif df.sha256 in seen_sha:
                logger.info(
                    "delta.duplicate_skipped", title=row.title, sha256=df.sha256
                )
            else:
                seen_sha.add(df.sha256)
                files.append(df)
        # Anything beyond the cap is reported as missing.
        for i, row in enumerate(rows[MAX_DOCS:], start=MAX_DOCS):
            missing.append(row.title or f"row {i}")

        if files:
            # render_incomplete (fewer rendered rows than Delta's "N items"
            # total) counts as partial even when every rendered row downloaded.
            status = (
                DownloadStatus.partial
                if (missing or render_incomplete)
                else DownloadStatus.complete
            )
            detail = None
            if render_incomplete:
                detail = (
                    f"Rendered {len(rows)} of {items_count} document rows Delta "
                    "lists; some rows may not have rendered."
                )
            return DownloadResult(
                status=status, files=files, missing=missing, detail=detail
            )

        # Rows were seen but NOT ONE downloaded — surface a clear error rather
        # than a silent nothing_available (which would hide a broken action-menu
        # selector).
        logger.info(
            "delta.download_all_failed", rows=len(rows), resp_id=resp_id,
            list_id=list_id,
        )
        return DownloadResult(
            status=DownloadStatus.error,
            missing=missing,
            detail=(
                f"Found {len(rows)} documents but could not trigger any "
                "downloads — Delta action-menu selector may have changed."
            ),
        )


def _ingest_bridge_file(
    rd,
    tender_id: int,
    *,
    title: str | None = None,
    file_type: str | None = None,
    source_url: str = "",
) -> DownloadedFile | None:
    """Read a click-captured bridge download from the shared volume, size-cap it,
    sha256 it, and copy it into the tender-documents storage layout. The on-disk
    record is named from the row's document title; the extension comes from the
    downloaded file, else the File Type column."""
    src = Path(settings.bridge_download_dir) / rd.path
    if not src.is_file():
        logger.info("delta.bridge_file_missing", path=str(src))
        return None
    size = src.stat().st_size
    if size > MAX_FILE_BYTES:
        logger.info("delta.oversize", path=str(src), size=size)
        return None
    data = src.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    ext = _ext_from(
        getattr(rd, "suggested_filename", None), rd.path, rd.mime_type, file_type
    )
    rel = Path(str(tender_id)) / sha[:2] / f"{sha}.{ext}"
    target = Path(settings.document_storage_dir) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(data)
    base = title or getattr(rd, "suggested_filename", None) or f"document.{ext}"
    filename = _safe_name(base)
    if "." not in filename:
        filename = f"{filename}.{ext}"
    return DownloadedFile(
        url=source_url,
        path=str(target),
        filename=filename,
        bytes=len(data),
        content_type=rd.mime_type,
        sha256=sha,
        storage_key=str(rel),
    )


def _safe_name(name: str) -> str:
    name = (name or "document").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name).lstrip("._")
    return name[:200] or "document"


# File Type column values map to an extension when the downloaded file has none.
_FILE_TYPE_EXTS = (
    "pdf", "docx", "doc", "xlsx", "xls", "pptx", "ppt",
    "zip", "csv", "txt", "rtf", "odt", "ods",
)
_MIME_EXTS = {
    "application/pdf": "pdf",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/zip": "zip",
}


def _ext_from(
    suggested: str | None,
    saved_path: str | None,
    mime: str | None,
    file_type: str | None,
) -> str:
    """Pick a file extension for a click-captured download: the file's own name
    first (the browser's suggested filename, then the saved name), then the MIME
    type, then the row's File Type column (e.g. "PDF"). Falls back to 'bin'."""
    for name in (suggested, saved_path):
        if name and "." in name:
            cand = name.rsplit(".", 1)[-1].lower()
            if cand.isalnum() and len(cand) <= 8:
                return cand
    if mime and mime.split(";")[0].strip().lower() in _MIME_EXTS:
        return _MIME_EXTS[mime.split(";")[0].strip().lower()]
    if file_type:
        ft = file_type.strip().lower()
        for ext in _FILE_TYPE_EXTS:
            if ext in ft:
                return ext
        token = re.match(r"[a-z0-9]+", ft)
        if token and len(token.group(0)) <= 8:
            return token.group(0)
    return "bin"
