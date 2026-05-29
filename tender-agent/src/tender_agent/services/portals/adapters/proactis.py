"""Proactis / ProContract adapter (platform_slug='procontract').

Platform: Proactis Sourcing, hosted at procontract.due-north.com (branded
"Proactis"). Login is username + password only — NO 2FA. As with Delta, a human
logs in by hand in the visible bridge window; this adapter NEVER submits
credentials.

This adapter mirrors the proven Delta blueprint (delta_esourcing.py) and reuses
ALL the same infrastructure — the browser bridge, the shared
PortalOrchestrator flow (login wait + the needs_user_confirmation pause +
_persist_documents + ensure_content_extracted). It does NOT fork orchestration
or write a Proactis-specific persistence/content path.

╔══════════════════════════════════════════════════════════════════════════╗
║  CONFIRMED FLOW (from live inspection + screenshots)                       ║
║  1. Login: https://procontract.due-north.com/Login/Index (no 2FA).         ║
║     Post-login home: /SupplierPostLoginHome. The supplier finds work via   ║
║     "Find opportunities" / "My activities"; an opportunity the supplier     ║
║     has expressed interest in becomes an "activity" reachable as            ║
║     /RfxResponse?rfxId={RFX_ID} — that activity page is where documents     ║
║     live.                                                                   ║
║  2. Interest flow (TWO buyer behaviours, both handled):                     ║
║     - Express Interest (analogous to Delta's Register Interest) signals     ║
║       intent to bid → the human-judgment pause applies here.                ║
║     - IMMEDIATE buyers release documents straight away (activity appears).  ║
║     - EMAIL-CONFIRMATION buyers require an out-of-band email confirmation   ║
║       before the activity/documents appear. The adapter cannot wait for an  ║
║       email, so it surfaces a clean "awaiting buyer release" state; the     ║
║       supplier re-runs the fetch once the activity appears.                 ║
║     - ALREADY-EXPRESSED-INTEREST (the test tender): the activity already    ║
║       exists → no express-interest step and no pause; fetch directly.       ║
║  3. The activity page has TWO document areas, both of which are collected:  ║
║     - "Activity documentation, files & links (N)" — a table whose Title     ║
║       column is a clickable link (Title / Type / Size).                     ║
║     - "Terms & conditions (N)" — a SEPARATE list with its own link(s).      ║
║                                                                            ║
║  Document links are plain anchors, so downloads use a two-step fallback:    ║
║  (1) PRIMARY — the link's href is fetched in the authenticated session via  ║
║      the bridge's cookie-aware /download endpoint; (2) FALLBACK — if the    ║
║      href is unusable or the fetch fails, the link is clicked and the        ║
║      resulting download captured (bridge /click-download).                  ║
║                                                                            ║
║  Selectors/markers marked CONFIRM SELECTOR are best-effort against the      ║
║  live DOM; the flow around them is real and tested with a mocked bridge.    ║
╚══════════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

import contextlib
import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

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


# --- Proactis / ProContract constants -----------------------------------
# CONFIRMED = verified against the live site (URLs + flow). CONFIRM SELECTOR =
# best-effort DOM markers; correcting these is the whole of any remaining
# fix-up, the logic around them is platform-agnostic.

PROACTIS_BASE = "https://procontract.due-north.com"

PROACTIS_URLS = {
    # CONFIRMED — supplier login page; navigating to a supplier page while
    # unauthenticated redirects here automatically.
    "login": f"{PROACTIS_BASE}/Login/Index",
    # CONFIRMED — the post-login supplier home; used as the authenticated probe
    # (an unauthenticated hit redirects to /Login/Index).
    "post_login_home": f"{PROACTIS_BASE}/SupplierPostLoginHome",
    # CONFIRM SELECTOR — the supplier "My activities" listing. Reached from the
    # post-login home; used to title-match an activity when no rfxId is known.
    "my_activities": f"{PROACTIS_BASE}/Activity/Index",
    # CONFIRMED — the activity (RfxResponse) page; %s = rfxId. The two document
    # sections live here.
    "activity": f"{PROACTIS_BASE}/RfxResponse?rfxId=%s",
    # BEST-EFFORT — supplier logout, used to release the session politely.
    "logout": f"{PROACTIS_BASE}/Login/Logout",
}

PROACTIS_SELECTORS = {
    # CONFIRM SELECTOR — a stable element that only renders for an authenticated
    # supplier (the left-nav items / the post-login home heading).
    "logged_in_marker": (
        "a:has-text('Find opportunities'), a:has-text('My activities'), "
        "a:has-text('Supplier'), :has-text('Supplier Post-Login Home')"
    ),
    # CONFIRM SELECTOR — the activity-documentation table + its data rows.
    "documents_table": "table#activityDocuments, table.activity-documents",
    "document_rows": (
        "table#activityDocuments tbody tr, table.activity-documents tbody tr"
    ),
    # CONFIRM SELECTOR — the Terms & conditions list (separate from the table).
    "terms_list": "#termsAndConditions, .terms-and-conditions",
    # CONFIRM SELECTOR — the Express Interest control on an opportunity.
    "express_interest_button": (
        "a:has-text('Express interest'), button:has-text('Express interest'), "
        "input[type='submit'][value*='Express interest' i]"
    ),
    "express_interest_button_text": "EXPRESS INTEREST",
}

# Section headings used to slice the activity page into its TWO document areas.
# The text is matched case-insensitively as a substring (real headings carry a
# trailing count, e.g. "Activity documentation, files & links (3)").
DOC_SECTION_MARKERS = ("activity documentation",)
TERMS_SECTION_MARKERS = ("terms & conditions", "terms and conditions")
# Headings/blocks that typically FOLLOW the document areas — used as the end
# boundary so trailing nav/footer anchors aren't mistaken for documents.
_SECTION_END_MARKERS = (
    "clarification",
    "messages",
    "activity timeline",
    "questions and answers",
    "powered by",
    "</body>",
    "©",
)

# Markers that prove the activity content rendered (so we can distinguish a
# slow render from "no documents yet"). CONFIRM SELECTOR.
ACTIVITY_RENDERED_MARKERS = (
    "activity documentation",
    "terms & conditions",
    "terms and conditions",
)

# "Expression of interest received — buyer must confirm before documents are
# released" indicators (the EMAIL-CONFIRMATION case). CONFIRM TEXT.
EMAIL_CONFIRMATION_MARKERS = (
    "you will receive an email",
    "email confirmation",
    "once the buyer",
    "awaiting",
    "pending approval",
    "has been sent to the buyer",
    "expression of interest has been received",
    "interest has been registered",
    "confirm your expression of interest",
)

# "Expression of interest was accepted / already expressed" indicators.
EOI_SUCCESS_MARKERS = (
    "expressed interest",
    "expression of interest",
    "successfully expressed",
    "you have already expressed",
    "interest registered",
)

EXPRESS_INTEREST_DETAIL = (
    "Proactis requires you to Express Interest in this opportunity before "
    "documents are released. This tells the buyer you intend to bid. Confirm "
    "to proceed."
)

ALREADY_INTERESTED_DETAIL = (
    "Your organisation has already expressed interest in this Proactis "
    "opportunity. Confirm to pull its documents — no further Express Interest "
    "is needed."
)

AWAITING_BUYER_RELEASE_DETAIL = (
    "Interest expressed. This buyer requires email confirmation before "
    "releasing documents. You'll receive an email shortly; once the activity "
    "appears in your account, re-run the fetch to pull the documents."
)

PROACTIS_DOMAIN_RE = re.compile(r"(^|\.)due-north\.com$", re.IGNORECASE)

# After the human logs in, ProContract lands them on a /Supplier* page.
PROACTIS_LOGIN_SUCCESS_PATTERN = r"due-north\.com/Supplier"

# --- rendered-DOM waits (the activity content is JS-rendered) -----------
PROACTIS_RENDER_TIMEOUT_MS = 15000
PROACTIS_RENDER_FALLBACK_TIMEOUT_MS = 6000
PROACTIS_AUTH_MARKER_WAIT_MS = 8000

MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_DOCS = 50

# Title fuzzy-match threshold for the My-activities listing (Jaccard overlap),
# only used to disambiguate when more than one activity is listed.
MATCH_MIN_SCORE = 0.15


# --- rfxId extraction ----------------------------------------------------
# The activity's rfxId rides in the tender's own portal URL (source_url /
# candidate document URLs / description). We read it from the tender data —
# never by scraping ProContract.
_RFX_ID_RE = re.compile(r"[?&]rfxId=([^&#\s\"'<>]+)", re.IGNORECASE)


def extract_rfx_id(*sources: str | None) -> str | None:
    """Return the Proactis rfxId found in any of the given strings (the tender's
    source_url, description, candidate URLs), or None. Sources are scanned in
    order; the first rfxId wins."""
    for src in sources:
        if not src:
            continue
        m = _RFX_ID_RE.search(src)
        if m:
            return m.group(1)
    return None


# --- HTML parsing helpers ------------------------------------------------
_TAG_RE = re.compile(r"(?s)<[^>]+>")
_ANCHOR_RE = re.compile(
    r"(?is)<a\b[^>]*?href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>"
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Anchors that are never DOCUMENTS (table-sort controls, contact emails). These
# are dropped while parsing the document sections.
_SKIP_ANCHOR_RE = re.compile(r"(?i)^mailto:|[?&]sort=")
# Hrefs that can't be GET-ed directly (fragment / script-triggered links). A
# document link of this shape is still a real document — it just needs the
# click-download fallback rather than the direct in-session GET.
_NON_DOC_HREF_RE = re.compile(r"(?i)^(#|javascript:|mailto:)")


@dataclass
class _ProactisDoc:
    """One document on the activity page. `href` is the link target (may be a
    relative URL); `section` is 'documentation' or 'terms'; `doc_type` is the
    Type column / extension hint when available."""

    title: str | None
    href: str | None
    section: str
    doc_type: str | None = None


def _clean_text(html_fragment: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", html_fragment or "")).strip()


def _section_slice(html: str, start_markers: Iterable[str]) -> str:
    """Return the HTML slice from the earliest start-marker heading to the next
    section/footer boundary (or end). Empty string if no start-marker present."""
    low = (html or "").lower()
    starts = [low.find(m) for m in start_markers if m in low]
    starts = [s for s in starts if s >= 0]
    if not starts:
        return ""
    start = min(starts)
    ends = [low.find(m, start + 1) for m in _SECTION_END_MARKERS]
    ends = [e for e in ends if e > start]
    # Also stop at the OTHER document section so a slice never swallows it.
    other = TERMS_SECTION_MARKERS if start_markers is DOC_SECTION_MARKERS else ()
    ends += [e for e in (low.find(m, start + 1) for m in other) if e > start]
    end = min(ends) if ends else len(html)
    return html[start:end]


def _ext_of(name: str | None) -> str | None:
    """Lowercased extension of a filename/href, if it looks like a real one."""
    if not name:
        return None
    tail = name.split("?")[0].split("#")[0].rsplit("/", 1)[-1]
    if "." in tail:
        cand = tail.rsplit(".", 1)[-1].lower()
        if cand.isalnum() and 1 <= len(cand) <= 8:
            return cand
    return None


def _anchors_in(html_slice: str, section: str) -> list[_ProactisDoc]:
    """Extract document links (title + href) from a section slice, skipping
    non-document anchors (sort/JS/fragment/mailto). Deduped by (href, title)."""
    out: list[_ProactisDoc] = []
    seen: set[tuple[str | None, str | None]] = set()
    for m in _ANCHOR_RE.finditer(html_slice or ""):
        href, inner = m.group(1).strip(), _clean_text(m.group(2))
        if not inner:
            continue
        if not href or _SKIP_ANCHOR_RE.search(href):
            continue
        key = (href, inner)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            _ProactisDoc(
                title=inner,
                href=href,
                section=section,
                doc_type=_ext_of(inner) or _ext_of(href),
            )
        )
    return out


def _parse_activity_documents(html: str) -> list[_ProactisDoc]:
    """Collect documents from BOTH activity areas: the "Activity documentation,
    files & links" table AND the separate "Terms & conditions" list. Returns
    documentation docs first, then terms docs."""
    doc_slice = _section_slice(html, DOC_SECTION_MARKERS)
    terms_slice = _section_slice(html, TERMS_SECTION_MARKERS)
    docs = _anchors_in(doc_slice, "documentation")
    terms = _anchors_in(terms_slice, "terms")
    # If the documentation slice failed to isolate (heading drift) but a doc
    # table exists, fall back to anchors inside any <table> so we don't miss the
    # files entirely.
    if not docs and "<table" in (html or "").lower():
        docs = _anchors_in(html, "documentation")
        # Don't double-count the terms anchors picked up by the whole-page scan.
        terms_hrefs = {(t.href, t.title) for t in terms}
        docs = [d for d in docs if (d.href, d.title) not in terms_hrefs]
    return docs + terms


# --- My-activities listing (title-match path) ---------------------------
@dataclass
class _ActivityRow:
    rfx_id: str
    title: str | None


_ACTIVITY_LINK_RE = re.compile(
    r"(?is)<a\b[^>]*?href=[\"']([^\"']*RfxResponse[^\"']*rfxId=([^&\"'#]+)[^\"']*)"
    r"[\"'][^>]*>(.*?)</a>"
)


def _parse_activity_rows(html: str) -> list[_ActivityRow]:
    """Enumerate the supplier's activities from a listing page by the
    RfxResponse?rfxId=… link pattern. One row per distinct rfxId; title is the
    link text."""
    rows: list[_ActivityRow] = []
    seen: set[str] = set()
    decoded = (html or "").replace("&amp;", "&")
    for m in _ACTIVITY_LINK_RE.finditer(decoded):
        rfx_id, inner = m.group(2), m.group(3)
        if rfx_id in seen:
            continue
        seen.add(rfx_id)
        rows.append(_ActivityRow(rfx_id=rfx_id, title=_clean_text(inner) or None))
    return rows


def _title_tokens(value: str | None) -> set[str]:
    return {tok for tok in _TOKEN_RE.findall((value or "").lower()) if len(tok) >= 3}


def _title_score(a: str | None, b: str | None) -> float:
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return 0.0
    union = len(ta | tb)
    return len(ta & tb) / union if union else 0.0


def _has_any(text: str | None, markers: Iterable[str]) -> bool:
    low = (text or "").lower()
    return any(m in low for m in markers)


def _is_login_url(url: str | None) -> bool:
    """True if the URL is ProContract's login page. Checks the PATH (so the
    post-login home — /SupplierPostLoginHome, which contains the substring
    'login' — is not mistaken for the login page)."""
    path = (urlparse(url or "").path or "").lower()
    return path.startswith("/login")


# File Type / extension → final extension fallback set.
_KNOWN_EXTS = (
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
    href: str | None,
    mime: str | None,
    doc_type: str | None,
) -> str:
    """Pick a file extension: the captured file's own name first, then the
    href, then the MIME type, then the Type column / parsed hint. 'bin' last."""
    for name in (suggested, saved_path, href):
        ext = _ext_of(name)
        if ext:
            return ext
    if mime and mime.split(";")[0].strip().lower() in _MIME_EXTS:
        return _MIME_EXTS[mime.split(";")[0].strip().lower()]
    if doc_type:
        dt = doc_type.strip().lower()
        for ext in _KNOWN_EXTS:
            if ext in dt:
                return ext
        token = re.match(r"[a-z0-9]+", dt)
        if token and len(token.group(0)) <= 8:
            return token.group(0)
    return "bin"


def _safe_name(name: str) -> str:
    name = (name or "document").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name).lstrip("._")
    return name[:200] or "document"


def _ingest_bridge_file(
    bf,
    tender_id: int,
    *,
    title: str | None,
    href: str | None,
    doc_type: str | None,
    source_url: str,
) -> DownloadedFile | None:
    """Read a bridge-captured download from the shared volume, size-cap it,
    sha256 it, and copy it into the tender-documents storage layout. The on-disk
    record is named from the document title; the extension comes from the
    downloaded file, then the href, then the MIME, then the Type column."""
    src = Path(settings.bridge_download_dir) / bf.path
    if not src.is_file():
        logger.info("proactis.bridge_file_missing", path=str(src))
        return None
    size = src.stat().st_size
    if size > MAX_FILE_BYTES:
        logger.info("proactis.oversize", path=str(src), size=size)
        return None
    data = src.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    ext = _ext_from(
        getattr(bf, "suggested_filename", None), bf.path, href,
        getattr(bf, "mime_type", None), doc_type,
    )
    rel = Path(str(tender_id)) / sha[:2] / f"{sha}.{ext}"
    target = Path(settings.document_storage_dir) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(data)
    base = title or getattr(bf, "suggested_filename", None) or f"document.{ext}"
    filename = _safe_name(base)
    if "." not in filename:
        filename = f"{filename}.{ext}"
    return DownloadedFile(
        url=source_url,
        path=str(target),
        filename=filename,
        bytes=len(data),
        content_type=getattr(bf, "mime_type", None),
        sha256=sha,
        storage_key=str(rel),
    )


class ProactisAdapter(PortalAdapter):
    """Proactis / ProContract (procontract.due-north.com)."""

    platform_slug = "procontract"
    requires_browser = False  # uses the Windows bridge, not an in-process page
    requires_login = True

    # Captured during locate / register; reused by the download step.
    _rfx_id: str | None = None
    _activity_url: str | None = None

    # --- classification / login ----------------------------------------

    def matches_url(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return bool(host) and bool(PROACTIS_DOMAIN_RE.search(host))

    def login_url(self) -> str | None:
        # Point the visible window at the supplier home; ProContract redirects an
        # unauthenticated user to /Login/Index automatically.
        return PROACTIS_URLS["post_login_home"]

    def login_success_pattern(self) -> str:
        return PROACTIS_LOGIN_SUCCESS_PATTERN

    def _rfx_sources(self, ctx: PortalContext) -> Iterable[str | None]:
        return (ctx.source_url, ctx.description, *ctx.candidate_urls, ctx.tender_ref)

    async def is_authenticated(self, ctx: PortalContext) -> bool:
        """Navigate to the supplier home; authenticated if we're not bounced to
        /Login and the logged-in supplier marker renders.

        Like Delta, the supplier chrome is JS-rendered, so we WAIT for the marker
        in the rendered DOM rather than probing once — an already-authenticated
        session that renders the menu a beat late must not read as 'fresh login
        required'. Logs reuse vs fresh-login."""
        bridge, slug = ctx.bridge, ctx.platform_slug
        if bridge is None or slug is None:
            return False
        try:
            await bridge.navigate(slug, PROACTIS_URLS["post_login_home"])
            status = await bridge.session_status(slug)
        except Exception as exc:  # noqa: BLE001
            logger.info("proactis.fresh_login_required", reason="navigate_failed",
                        error=str(exc))
            return False
        if _is_login_url(status.get("current_url")):
            logger.info("proactis.fresh_login_required", reason="redirected_to_login")
            return False
        try:
            res = await bridge.rendered_html(
                slug,
                wait_for_selector=PROACTIS_SELECTORS["logged_in_marker"],
                timeout_ms=PROACTIS_AUTH_MARKER_WAIT_MS,
            )
            marker = res.wait_satisfied
            if not marker and _is_login_url(res.current_url):
                logger.info(
                    "proactis.fresh_login_required", reason="redirected_to_login"
                )
                return False
        except Exception:  # noqa: BLE001
            marker = False
        if marker:
            logger.info("proactis.session_reused", slug=slug)
            return True
        logger.info("proactis.fresh_login_required", reason="no_logged_in_marker")
        return False

    async def authenticate(
        self, ctx: PortalContext, creds: Credentials | None
    ) -> AuthResult:
        # Login is performed by the human in the visible window (username +
        # password, no 2FA). The adapter never submits credentials.
        return AuthResult(status=AuthStatus.success, detail="human login (no creds)")

    # --- locate / interest ---------------------------------------------

    async def _read_activity_html(self, bridge, slug: str) -> str:
        """Read the RENDERED activity DOM, waiting for the documentation table
        (the surest render signal), falling back to the section-heading text."""
        try:
            res = await bridge.rendered_html(
                slug,
                wait_for_selector=PROACTIS_SELECTORS["document_rows"],
                timeout_ms=PROACTIS_RENDER_TIMEOUT_MS,
            )
            if res.html:
                return res.html
        except Exception as exc:  # noqa: BLE001
            logger.info("proactis.rendered_html_failed", where="activity", error=str(exc))
        best = ""
        for marker in ACTIVITY_RENDERED_MARKERS:
            try:
                res = await bridge.rendered_html(
                    slug, wait_for_text=marker,
                    timeout_ms=PROACTIS_RENDER_FALLBACK_TIMEOUT_MS,
                )
            except Exception:  # noqa: BLE001
                continue
            if res.html:
                best = res.html
            if res.wait_satisfied:
                return res.html or best
        return best

    async def _activity_available(self, html: str) -> bool:
        """True if the rendered activity page shows real activity content (a
        document section heading or a parsed document), i.e. the activity exists
        and documents are released."""
        if _has_any(html, ACTIVITY_RENDERED_MARKERS):
            return True
        return bool(_parse_activity_documents(html))

    async def _match_in_activities(
        self, bridge, slug: str, ctx: PortalContext
    ) -> _ActivityRow | None:
        """Find THIS tender in the supplier's My-activities listing by title
        (Jaccard token overlap). A single listed activity is unambiguous; with
        several, pick the best above MATCH_MIN_SCORE. Logs what matched."""
        try:
            await bridge.navigate(slug, PROACTIS_URLS["my_activities"])
            res = await bridge.rendered_html(
                slug, wait_for_text="RfxResponse",
                timeout_ms=PROACTIS_RENDER_TIMEOUT_MS,
            )
            html = res.html or ""
        except Exception as exc:  # noqa: BLE001
            logger.info("proactis.activities_read_failed", error=str(exc))
            return None
        rows = _parse_activity_rows(html)
        if not rows:
            return None
        title = ctx.title
        if not title:
            if len(rows) == 1:
                return rows[0]
            return None
        scored = sorted(
            ((row, _title_score(title, row.title)) for row in rows),
            key=lambda pair: pair[1], reverse=True,
        )
        best, score = scored[0]
        threshold = 0.0 if len(rows) == 1 else MATCH_MIN_SCORE
        if score > threshold:
            logger.info(
                "proactis.activity_match", rfx_id=best.rfx_id,
                row_title=best.title, score=round(score, 3), rows=len(rows),
            )
            return best
        logger.info(
            "proactis.activity_no_match", best_title=best.title,
            best_score=round(score, 3), rows=len(rows),
        )
        return None

    async def locate_tender(
        self, ctx: PortalContext, tender_ref: str
    ) -> LocateResult:
        """Locate the tender's activity.

        Resolution order (per the spec):
          1. If the tender carries an rfxId, open its activity page. If activity
             content is present → FOUND (already-interested → no pause, fetch
             directly). If an email-confirmation indicator shows → pause with the
             awaiting-buyer-release detail (resumable: re-run once released).
          2. Otherwise title-match the My-activities listing; a hit → FOUND.
          3. Otherwise the supplier hasn't expressed interest → gate on Express
             Interest (requires_interest_first → the needs_user_confirmation
             pause; the Express Interest click is user-gated, intent to bid)."""
        bridge, slug = ctx.bridge, ctx.platform_slug
        if bridge is None or slug is None:
            return LocateResult(status=LocateStatus.error, detail="no bridge session")

        rfx_id = extract_rfx_id(*self._rfx_sources(ctx))

        # 1. Known rfxId → open the activity page directly.
        if rfx_id:
            self._rfx_id = rfx_id
            activity_url = PROACTIS_URLS["activity"] % rfx_id
            try:
                await bridge.navigate(slug, activity_url)
            except Exception as exc:  # noqa: BLE001
                return LocateResult(status=LocateStatus.error, detail=str(exc))
            html = await self._read_activity_html(bridge, slug)
            if await self._activity_available(html):
                self._activity_url = activity_url
                logger.info(
                    "proactis.already_interested", slug=slug, rfx_id=rfx_id
                )
                return LocateResult(
                    status=LocateStatus.found,
                    tender_url=activity_url,
                    detail=ALREADY_INTERESTED_DETAIL,
                    already_authorized=True,
                )
            # Activity not available yet — distinguish "awaiting buyer release"
            # (expressed, email pending) from "not expressed yet".
            try:
                text = await bridge.page_text(slug)
            except Exception:  # noqa: BLE001
                text = ""
            if _has_any(html, EMAIL_CONFIRMATION_MARKERS) or _has_any(
                text, EMAIL_CONFIRMATION_MARKERS
            ):
                logger.info("proactis.awaiting_buyer_release", slug=slug, rfx_id=rfx_id)
                return LocateResult(
                    status=LocateStatus.requires_interest_first,
                    tender_url=activity_url,
                    detail=AWAITING_BUYER_RELEASE_DETAIL,
                )

        # 2. No rfxId (or its activity isn't available) → title-match the
        #    My-activities listing.
        match = await self._match_in_activities(bridge, slug, ctx)
        if match is not None:
            self._rfx_id = match.rfx_id
            self._activity_url = PROACTIS_URLS["activity"] % match.rfx_id
            logger.info(
                "proactis.already_interested", slug=slug, rfx_id=match.rfx_id,
                via="activity_match",
            )
            return LocateResult(
                status=LocateStatus.found,
                tender_url=self._activity_url,
                detail=ALREADY_INTERESTED_DETAIL,
                already_authorized=True,
            )

        # 3. Not expressed interest yet → gate on Express Interest (the pause).
        opportunity_url = ctx.source_url or (
            PROACTIS_URLS["activity"] % rfx_id if rfx_id else None
        )
        if opportunity_url is None:
            return LocateResult(
                status=LocateStatus.not_found,
                detail=(
                    "No Proactis rfxId or opportunity URL found for this tender, "
                    "and it is not in your activities."
                ),
            )
        logger.info("proactis.express_interest_needed", slug=slug, rfx_id=rfx_id)
        return LocateResult(
            status=LocateStatus.requires_interest_first,
            tender_url=opportunity_url,
            detail=EXPRESS_INTEREST_DETAIL,
        )

    async def register_interest(self, ctx: PortalContext) -> RegisterResult:
        """Express interest in the opportunity.

        Called by the orchestrator ONLY after the user confirms via the
        needs_user_confirmation pause — never automatically (it signals intent
        to bid). Navigates to the opportunity and clicks Express Interest, then
        reads the result. Either outcome returns success (interest IS expressed);
        download_documents decides whether documents are available now
        (IMMEDIATE) or the buyer must confirm by email (the awaiting state)."""
        bridge, slug = ctx.bridge, ctx.platform_slug
        if bridge is None or slug is None:
            return RegisterResult(status=RegisterStatus.error, detail="no bridge")

        opportunity_url = ctx.source_url or (
            PROACTIS_URLS["activity"] % self._rfx_id if self._rfx_id else None
        )
        if opportunity_url:
            try:
                await bridge.navigate(slug, opportunity_url)
            except Exception as exc:  # noqa: BLE001
                return RegisterResult(status=RegisterStatus.error, detail=str(exc))

        # If interest was already expressed (no Express Interest control), don't
        # re-click — treat as already registered.
        try:
            has_button = await bridge.element_exists(
                slug, PROACTIS_SELECTORS["express_interest_button"]
            )
        except Exception:  # noqa: BLE001
            has_button = False
        if not has_button:
            text = ""
            with contextlib.suppress(Exception):
                text = await bridge.page_text(slug)
            if _has_any(text, EOI_SUCCESS_MARKERS):
                logger.info("proactis.express_interest_skipped",
                            slug=slug, reason="already_expressed")
                return RegisterResult(status=RegisterStatus.already_registered)
            return RegisterResult(
                status=RegisterStatus.error,
                detail="Express Interest control not found on the opportunity page.",
            )

        try:
            await bridge.click(slug, PROACTIS_SELECTORS["express_interest_button"])
        except Exception as exc:  # noqa: BLE001
            return RegisterResult(status=RegisterStatus.error, detail=str(exc))
        logger.info("proactis.express_interest_clicked", slug=slug)

        text = ""
        with contextlib.suppress(Exception):
            text = await bridge.page_text(slug)
        if _has_any(text, EMAIL_CONFIRMATION_MARKERS):
            logger.info("proactis.express_interest_email_pending", slug=slug)
        return RegisterResult(status=RegisterStatus.success)

    async def logout(self, ctx: PortalContext) -> bool:
        """Best-effort logout to release the session. Non-fatal."""
        bridge, slug = ctx.bridge, ctx.platform_slug
        if bridge is None or slug is None:
            return False
        ok = False
        try:
            await bridge.navigate(slug, PROACTIS_URLS["logout"])
            ok = True
        except Exception as exc:  # noqa: BLE001
            logger.info("proactis.logout_failed", error=str(exc))
        logger.info("proactis.logout", slug=slug, ok=ok)
        return ok

    # --- download ------------------------------------------------------

    def _resolve_activity_url(self, ctx: PortalContext) -> str | None:
        if self._activity_url:
            return self._activity_url
        if self._rfx_id:
            return PROACTIS_URLS["activity"] % self._rfx_id
        rfx_id = extract_rfx_id(*self._rfx_sources(ctx))
        if rfx_id:
            self._rfx_id = rfx_id
            return PROACTIS_URLS["activity"] % rfx_id
        return None

    async def _download_one(
        self, bridge, slug: str, doc: _ProactisDoc, activity_url: str
    ):
        """Download a single document: PRIMARY direct-link (cookie-aware
        in-session GET of the href) → FALLBACK click+expect_download on the link.
        Returns the bridge file descriptor, or None if both paths failed."""
        href_abs = None
        if doc.href and not _NON_DOC_HREF_RE.search(doc.href):
            href_abs = urljoin(activity_url, doc.href)
        # PRIMARY — direct link, fetched in the authenticated session.
        if href_abs and href_abs.lower().startswith("http"):
            try:
                return await bridge.download(slug, href_abs)
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "proactis.direct_download_failed", title=doc.title,
                    href=href_abs, error=str(exc),
                )
        # FALLBACK — click the link by its visible text and capture the download.
        if doc.title:
            safe_text = doc.title.replace("'", " ").strip()
            selector = f"a:has-text('{safe_text}')"
            try:
                return await bridge.click_download(slug, selector)
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "proactis.click_download_failed", title=doc.title,
                    error=str(exc),
                )
        return None

    async def download_documents(
        self, ctx: PortalContext, dest_dir: str
    ) -> DownloadResult:
        """Download every document from BOTH activity sections (Activity
        documentation table + Terms & conditions list).

        Each document is fetched direct-link-first (the href is GET-ed in the
        authenticated session) with a click-download fallback, then routed
        through the SHARED persistence + content extraction (chunk-3 caps +
        sha256 dedup). Status is partial when some documents fail; a CLEAR error
        (never a silent nothing_available) when documents were seen but NONE
        could be downloaded. When no documents render and an email-confirmation
        indicator is present, returns nothing_available with the
        awaiting-buyer-release detail (a clean paused state, not a failure)."""
        bridge, slug = ctx.bridge, ctx.platform_slug
        if bridge is None or slug is None:
            return DownloadResult(status=DownloadStatus.error, detail="no bridge session")

        activity_url = self._resolve_activity_url(ctx)
        if not activity_url:
            return DownloadResult(
                status=DownloadStatus.error,
                detail="could not determine the Proactis activity (no rfxId)",
            )
        try:
            await bridge.navigate(slug, activity_url)
        except Exception as exc:  # noqa: BLE001
            return DownloadResult(status=DownloadStatus.error, detail=str(exc))

        html = await self._read_activity_html(bridge, slug)
        docs = _parse_activity_documents(html)
        if not docs:
            # No documents. Distinguish "buyer must confirm by email" (a clean
            # awaiting state) from "activity didn't render".
            try:
                text = await bridge.page_text(slug)
            except Exception:  # noqa: BLE001
                text = ""
            if _has_any(html, EMAIL_CONFIRMATION_MARKERS) or _has_any(
                text, EMAIL_CONFIRMATION_MARKERS
            ):
                logger.info("proactis.awaiting_buyer_release", slug=slug,
                            rfx_id=self._rfx_id)
                return DownloadResult(
                    status=DownloadStatus.nothing_available,
                    detail=AWAITING_BUYER_RELEASE_DETAIL,
                )
            logger.info("proactis.documents_none", slug=slug, rfx_id=self._rfx_id)
            return DownloadResult(
                status=DownloadStatus.nothing_available,
                detail=(
                    "Activity page rendered no document links in either the "
                    "documentation table or the Terms & conditions list — "
                    "Proactis may have changed its page structure."
                ),
            )

        doc_count = sum(1 for d in docs if d.section == "documentation")
        terms_count = sum(1 for d in docs if d.section == "terms")
        logger.info(
            "proactis.documents_enumerated", slug=slug, rfx_id=self._rfx_id,
            documentation=doc_count, terms=terms_count, total=len(docs),
        )

        files: list[DownloadedFile] = []
        seen_sha: set[str] = set()
        missing: list[str] = []
        ingest_failures = 0
        for i, doc in enumerate(docs[:MAX_DOCS]):
            label = doc.title or f"{doc.section} {i}"
            bf = await self._download_one(bridge, slug, doc, activity_url)
            if bf is None:
                missing.append(label)
                continue
            # Distinct, non-null source_url per file so they never collide on the
            # tender_document_files (tender_id, url) unique constraint. The href
            # is naturally distinct; click-fallback docs without a usable href
            # get a per-doc fragment off the activity URL.
            href_abs = (
                urljoin(activity_url, doc.href)
                if doc.href and not _NON_DOC_HREF_RE.search(doc.href)
                else None
            )
            source_url = href_abs or (
                f"{activity_url}#{i + 1}-{_safe_name(doc.title or doc.section)}"
            )
            df = _ingest_bridge_file(
                bf, ctx.tender_id, title=doc.title, href=href_abs,
                doc_type=doc.doc_type, source_url=source_url,
            )
            if df is None:
                logger.info("proactis.download_ingest_failed", title=doc.title)
                ingest_failures += 1
                missing.append(label)
            elif df.sha256 in seen_sha:
                logger.info("proactis.duplicate_skipped", title=doc.title,
                            sha256=df.sha256)
            else:
                seen_sha.add(df.sha256)
                files.append(df)
        for i, doc in enumerate(docs[MAX_DOCS:], start=MAX_DOCS):
            missing.append(doc.title or f"{doc.section} {i}")

        if files:
            status = (
                DownloadStatus.partial if missing else DownloadStatus.complete
            )
            return DownloadResult(status=status, files=files, missing=missing)

        # Documents were seen but NOT ONE produced a usable file — clear error.
        logger.info(
            "proactis.download_all_failed", slug=slug, docs=len(docs),
            ingest_failures=ingest_failures, rfx_id=self._rfx_id,
        )
        if ingest_failures:
            detail = (
                f"Captured {ingest_failures} document(s) but could not read them "
                "back from the bridge-downloads volume — check the shared mount "
                "(TENDER_AGENT_BRIDGE_DOWNLOAD_DIR)."
            )
        else:
            detail = (
                f"Found {len(docs)} documents but could not download any — the "
                "Proactis document-link selectors may have changed."
            )
        return DownloadResult(
            status=DownloadStatus.error, missing=missing, detail=detail
        )
