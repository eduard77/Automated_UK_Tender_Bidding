"""Proactis (procontract.due-north.com) opportunity discovery — public pages.

The Proactis "Find Opportunities" listing is fully PUBLIC — no login required:

    https://procontract.due-north.com/Opportunities/Index?tabName=opportunities&resetFilter=True

It is plain server-rendered HTML: a paginated table whose rows each carry the
title, buyer (OrgName), expression start/end dates, estimated value, a link to
the advert (``/Advert?advertId={GUID}``) and — crucially — the **DN reference**
(e.g. ``DN817867``) in the advert link's ``title`` attribute. Filters and paging
are URL-query-parameter driven.

So discovery fetches those public pages directly over plain HTTP (the same
``httpx`` convention the CF/FTS adapters use) and walks the pages. There is NO
bridge, NO Playwright, NO login, NO session. Login belongs ONLY to the later
document-fetch stage (``services.portals.adapters.proactis``), which is
human-gated and untouched by this module.

Pulls opportunities INTO the ``tenders`` table so the unified search finds them
alongside CF/FTS, deduped by ``procurement_ref`` (the DN reference). Reuses,
unchanged:

- ``services.ingestion._upsert_tender`` — UPSERT + change-hash + first/last seen.
- ``services.deduplicator.find_duplicate`` — cross-source dedup by
  ``procurement_ref`` (+ fuzzy buyer/title/value fallback), called inside
  ``_upsert_tender``.
- ``services.regions.resolve_for_tender`` — canonical region, called inside
  ``_upsert_tender``.

Dedup contract (unchanged from the previous bridge-based discovery):
- ``source_code  = "PROACTIS"``
- ``source_ref   = advertId`` (GUID, unique within Proactis)
- ``procurement_ref = DN reference`` — the cross-source dedup key, so a tender
  also on Contracts Finder merges on its DN.
- region resolved via the shared resolver.

The DN/value/date regexes are reused from the document-fetch adapter so the two
stages agree on what a DN reference / money / date looks like.
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.config import settings
from tender_agent.db import SessionLocal
from tender_agent.models import PollRun, Source
from tender_agent.schemas import NormalisedTender
from tender_agent.services.discovery.proactis_filter_config import (
    ProactisFilterConfig,
)
from tender_agent.services.ingestion import _upsert_tender
from tender_agent.services.portals.adapters.proactis import (
    OPPORTUNITY_DATE_RE,
    OPPORTUNITY_ID_RE,
    OPPORTUNITY_VALUE_RE,
    PROACTIS_BASE,
)

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Tender.source_code value for Proactis-discovered opportunities. Distinct
#: from the platform_slug "procontract" the document-fetch adapter uses —
#: source_code identifies the TENDER's origin; platform_slug identifies the
#: PORTAL adapter that can fetch its documents.
PROACTIS_SOURCE_CODE = "PROACTIS"

#: The public "Find Opportunities" listing (no login). Filters + paging are
#: applied as query parameters by ``build_listing_url``.
PROACTIS_OPPORTUNITIES_URL = f"{PROACTIS_BASE}/Opportunities/Index"

#: The public advert (opportunity detail) page; ``advertId`` is the GUID from
#: the listing row. Stored as the tender's ``source_url``.
PROACTIS_ADVERT_URL = f"{PROACTIS_BASE}/Advert?advertId=%s"

#: Rows fetched per page. The listing accepts a page-size param; a larger size
#: means fewer round-trips. Bounded so a single fetch stays reasonable.
LISTING_PAGE_SIZE = 100


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class DiscoveredOpportunity:
    """Everything one public listing row gives us about an opportunity.

    All fields except ``advert_id`` are best-effort: a row that omits the value
    or renders the buyer oddly doesn't break ingestion, it just yields a
    ``NormalisedTender`` with fewer fields populated. ``advert_id`` (source_ref)
    and ``dn_reference`` (procurement_ref) are the dedup-critical pair and both
    come straight off the row's advert link.
    """

    advert_id: str  # the GUID from the listing link; source_ref
    dn_reference: str | None  # "DN817867" from the link title attr; procurement_ref
    title: str
    buyer_name: str | None = None
    buyer_region: str | None = None  # raw text if the row exposes it
    value_amount: Decimal | None = None
    value_currency: str = "GBP"
    expression_start: datetime | None = None
    expression_end: datetime | None = None
    contract_start: date | None = None
    contract_end: date | None = None
    description: str | None = None
    categories: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    detail_url: str | None = None

    def is_complete_for_dedup(self) -> bool:
        """A row is dedup-ready iff it has the DN reference, the cross-source key."""
        return bool(self.dn_reference)


@dataclass
class DiscoveryRunResult:
    """Summary of a single discovery run. Returned by ``run()`` and used by both
    the admin endpoint and the scheduler to build a structured log line."""

    status: str  # "ok" | "error"
    pages_walked: int = 0
    rows_seen: int = 0
    opportunities_inserted: int = 0
    opportunities_updated: int = 0
    opportunities_unchanged: int = 0
    opportunities_deduped: int = 0  # row had a CF/FTS sibling by procurement_ref
    error: str | None = None
    poll_run_id: int | None = None


#: A page fetcher: takes a URL, returns the page HTML. Injectable so tests feed
#: saved HTML strings with no network.
Fetcher = Callable[[str], Awaitable[str]]


# ---------------------------------------------------------------------------
# Filter -> URL mapping
# ---------------------------------------------------------------------------


def build_listing_url(
    config: ProactisFilterConfig, page: int, page_size: int = LISTING_PAGE_SIZE
) -> str:
    """Build the public listing URL for one page, applying the filters that map
    cleanly to public query parameters.

    Mapping (see the PR description for the rationale and the dimensions that
    can't be expressed as public params):

    - ``resetFilter=True`` + ``tabName=opportunities`` — land on a clean,
      open-opportunities listing (the default view is open-only).
    - ``page`` / ``pageSize`` — pagination.
    - ``sortColumn`` / ``sortOrder`` — a stable sort so pagination is
      deterministic across pages.
    - ``keywords`` -> ``searchKeyword`` (only when non-empty).

    ``regions`` / ``categories`` / ``portals`` / ``organisations`` are NOT in the
    URL: the public listing selects those by the site's internal numeric IDs via
    its autocomplete, which our label-based config can't address without the
    authenticated flow we removed. They're handled best-effort in the pipeline
    (see ``_passes_pipeline_filters``) and documented in the PR.
    """
    params: list[tuple[str, str]] = [
        ("tabName", "opportunities"),
        ("resetFilter", "True"),
        ("page", str(page)),
        ("pageSize", str(page_size)),
        ("sortColumn", "Title"),
        ("sortOrder", "asc"),
    ]
    if config.keywords.strip():
        params.append(("searchKeyword", config.keywords.strip()))
    return f"{PROACTIS_OPPORTUNITIES_URL}?{urlencode(params)}"


def _passes_pipeline_filters(
    opp: DiscoveredOpportunity, config: ProactisFilterConfig
) -> bool:
    """Best-effort, listing-data-only fallback for filter dimensions that can't
    be expressed as public URL params.

    Only dimensions we can apply WITHOUT false drops are enforced here:

    - ``organisations`` — matched (case-insensitive substring) against the
      buyer/OrgName on the row. This is meaningful: organisation == buyer.
    - ``keywords`` — already applied via the URL's ``searchKeyword``; re-checked
      as a cheap substring safety net against title + buyer in case the upstream
      param is ignored.

    ``regions`` / ``categories`` / ``portals`` are deliberately NOT filtered: the
    public listing row doesn't reliably carry region/category, so substring
    matching would drop valid rows. They are left to the unified search's own
    region facet downstream. See the PR description.
    """
    haystack = f"{opp.title or ''} {opp.buyer_name or ''}".lower()

    if config.organisations:
        buyer = (opp.buyer_name or "").lower()
        if not any(org.strip().lower() in buyer for org in config.organisations if org.strip()):
            return False

    keyword = config.keywords.strip().lower()
    return not keyword or keyword in haystack


# ---------------------------------------------------------------------------
# Listing parse
# ---------------------------------------------------------------------------

_ROW_RE = re.compile(r"<tr\b[^>]*>(?P<row>.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<t[dh]\b[^>]*>(?P<cell>.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# The advert link in a row: <a ... href="...advertId=GUID..." ... title="DN..">Title</a>.
# We capture the whole opening-tag attribute blob so we can read both the
# advertId (from href) and the DN reference (from the title attribute),
# regardless of attribute order.
_ADVERT_ANCHOR_RE = re.compile(
    r"<a\b(?P<attrs>[^>]*\badvertId=(?P<advert_id>[0-9a-fA-F-]{8,})[^>]*)>"
    r"(?P<inner>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_TITLE_ATTR_RE = re.compile(r'\btitle\s*=\s*"(?P<title>[^"]*)"', re.IGNORECASE)
_NA_RE = re.compile(r"\bN/?A\b", re.IGNORECASE)


def _clean(html_fragment: str) -> str:
    """Strip tags + collapse whitespace + unescape the few entities Proactis uses."""
    text = _TAG_RE.sub(" ", html_fragment)
    text = (
        text.replace("&amp;", "&")
        .replace("&nbsp;", " ")
        .replace("&#163;", "£")
        .replace("&pound;", "£")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
    )
    return _WS_RE.sub(" ", text).strip()


def _parse_value(text: str) -> Decimal | None:
    """Parse "£1,400,000.00" -> Decimal; "N/A" / no money -> None."""
    m = OPPORTUNITY_VALUE_RE.search(text)
    if not m:
        return None
    try:
        return Decimal(m.group(1).replace(",", ""))
    except InvalidOperation:
        return None


def _parse_date(text: str) -> datetime | None:
    m = OPPORTUNITY_DATE_RE.search(text)
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return datetime(year, month, day, tzinfo=UTC)
    except ValueError:
        return None


def parse_listing_rows(html: str) -> list[DiscoveredOpportunity]:
    """Parse every opportunity row out of one public listing page.

    For each ``<tr>`` that contains an advert link we extract:

    - ``advert_id`` — the GUID from the link's ``advertId`` query param.
    - ``dn_reference`` — the ``DN…`` reference, read from the link's ``title``
      attribute (falling back to the row text if a tenant renders it elsewhere).
    - ``title`` — the link's anchor text.
    - ``buyer_name`` — the first non-title, non-date, non-value cell.
    - ``value_amount`` — the first "£…" money cell ("N/A" -> None).
    - ``expression_start`` / ``expression_end`` — the row's dates in order
      (one date -> treated as the closing/end date).
    """
    rows: list[DiscoveredOpportunity] = []
    for row_match in _ROW_RE.finditer(html):
        row_html = row_match.group("row")
        anchor = _ADVERT_ANCHOR_RE.search(row_html)
        if anchor is None:
            continue  # header / spacer / non-opportunity row

        advert_id = anchor.group("advert_id")
        title = _clean(anchor.group("inner"))
        if not title:
            continue

        # DN reference: prefer the link's title attribute, fall back to row text.
        dn_reference: str | None = None
        title_attr = _TITLE_ATTR_RE.search(anchor.group("attrs"))
        if title_attr:
            dn_match = OPPORTUNITY_ID_RE.search(title_attr.group("title"))
            if dn_match:
                dn_reference = dn_match.group(0)
        if dn_reference is None:
            dn_match = OPPORTUNITY_ID_RE.search(_clean(row_html))
            dn_reference = dn_match.group(0) if dn_match else None

        # Per-cell extraction for the best-effort fields.
        cells = [_clean(c.group("cell")) for c in _CELL_RE.finditer(row_html)]
        value_amount: Decimal | None = None
        dates: list[datetime] = []
        buyer_name: str | None = None
        for cell in cells:
            if not cell or title and cell == title:
                continue
            if value_amount is None and ("£" in cell or _NA_RE.fullmatch(cell)):
                value_amount = _parse_value(cell)
                continue
            cell_date = _parse_date(cell)
            if cell_date is not None:
                dates.append(cell_date)
                continue
            if buyer_name is None and not _NA_RE.fullmatch(cell):
                buyer_name = cell[:512]

        expression_start = dates[0] if len(dates) >= 2 else None
        expression_end = dates[-1] if dates else None

        rows.append(
            DiscoveredOpportunity(
                advert_id=advert_id,
                dn_reference=dn_reference,
                title=title,
                buyer_name=buyer_name,
                value_amount=value_amount,
                expression_start=expression_start,
                expression_end=expression_end,
                detail_url=PROACTIS_ADVERT_URL % advert_id,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Listing walk (HTTP, public, no login)
# ---------------------------------------------------------------------------

# Module-level so `run()` can read it after the async generator finishes without
# a class wrapper. Updated only by `_walk_listing`.
_last_walked_pages = 0


async def _walk_listing(
    fetch: Fetcher, config: ProactisFilterConfig, *, page_size: int = LISTING_PAGE_SIZE
):
    """Yield one ``DiscoveredOpportunity`` per row across every page, following
    the ``page`` query parameter up to ``config.max_pages``.

    Stops when: a page yields no NEW rows (empty page, or the site repeats the
    last page), the page is short (fewer rows than ``page_size`` -> last page),
    or the ``max_pages`` cap is hit.
    """
    global _last_walked_pages
    _last_walked_pages = 0
    seen_advert_ids: set[str] = set()

    for page in range(1, config.max_pages + 1):
        url = build_listing_url(config, page, page_size)
        html = await fetch(url)
        _last_walked_pages = page

        parsed = parse_listing_rows(html)
        new_rows = [r for r in parsed if r.advert_id not in seen_advert_ids]
        for row in new_rows:
            seen_advert_ids.add(row.advert_id)
            yield row

        logger.info(
            "discovery.proactis.page",
            page=page,
            rows=len(parsed),
            new_rows=len(new_rows),
        )

        if not new_rows:
            break  # empty or fully-repeated page -> end of listing
        if len(parsed) < page_size:
            break  # short page -> last page


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


async def run(
    *,
    config: ProactisFilterConfig,
    fetch: Fetcher | None = None,
    db_factory=SessionLocal,
) -> DiscoveryRunResult:
    """Run one Proactis discovery cycle over the public listing and return a
    structured summary.

    ``fetch`` is injectable for tests; production passes None and a plain
    ``httpx``-based fetcher (matching the CF/FTS adapter convention) is used.
    ``db_factory`` is injectable for tests; production uses ``SessionLocal``.
    Progress is committed per-opportunity so a mid-run failure still persists
    what it found.
    """
    result = DiscoveryRunResult(status="ok")

    # Open a PollRun row up front so even an early failure is auditable.
    with db_factory() as db:
        source = _ensure_proactis_source(db)
        poll_run = PollRun(source_id=source.id, status="running")
        db.add(poll_run)
        db.commit()
        db.refresh(poll_run)
        result.poll_run_id = poll_run.id

    logger.info(
        "discovery.proactis.start",
        poll_run_id=result.poll_run_id,
        constrained=config.is_constrained(),
        max_pages=config.max_pages,
    )

    client: httpx.AsyncClient | None = None
    if fetch is None:
        client = httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            headers={
                "User-Agent": settings.http_user_agent,
                "Accept": "text/html,application/xhtml+xml",
            },
            follow_redirects=True,
        )

        async def _default_fetch(url: str) -> str:
            response = await client.get(url)
            response.raise_for_status()
            return response.text

        fetch = _default_fetch

    try:
        with db_factory() as db:
            async for row in _walk_listing(fetch, config):
                result.rows_seen += 1
                if not row.is_complete_for_dedup():
                    logger.warning(
                        "discovery.proactis.row_missing_dn", advert_id=row.advert_id
                    )
                if not _passes_pipeline_filters(row, config):
                    continue
                action, deduped = _upsert_from_discovered(db, row)
                if action == "new":
                    result.opportunities_inserted += 1
                elif action == "updated":
                    result.opportunities_updated += 1
                else:
                    result.opportunities_unchanged += 1
                if deduped:
                    result.opportunities_deduped += 1
                db.commit()
        result.pages_walked = _last_walked_pages
    except Exception as exc:  # noqa: BLE001 — surfaced via PollRun
        result.status = "error"
        result.error = f"{type(exc).__name__}: {exc}"
        result.pages_walked = _last_walked_pages
        logger.exception("discovery.proactis.failed", poll_run_id=result.poll_run_id)
    finally:
        if client is not None:
            await client.aclose()
        _finalise_poll_run(db_factory, result)

    logger.info(
        "discovery.proactis.complete",
        poll_run_id=result.poll_run_id,
        status=result.status,
        pages=result.pages_walked,
        rows=result.rows_seen,
        inserted=result.opportunities_inserted,
        updated=result.opportunities_updated,
        unchanged=result.opportunities_unchanged,
        deduped=result.opportunities_deduped,
    )
    return result


# ---------------------------------------------------------------------------
# Upsert — funnels through the SAME path CF/FTS use
# ---------------------------------------------------------------------------


def _upsert_from_discovered(
    db: Session, opp: DiscoveredOpportunity
) -> tuple[str, bool]:
    """Convert a ``DiscoveredOpportunity`` into a ``NormalisedTender`` and feed
    it through the existing CF/FTS upsert path.

    Returns (action, deduped) where:
      - action in {"new", "updated", "unchanged"} — from ``_upsert_tender``.
      - deduped is True iff this new row was linked to an existing CF/FTS record
        via ``duplicate_of_id`` (cross-source dedup happened).
    """
    normalised = NormalisedTender(
        source_code=PROACTIS_SOURCE_CODE,
        source_ref=opp.advert_id,
        source_url=opp.detail_url or (PROACTIS_ADVERT_URL % opp.advert_id),
        procurement_ref=opp.dn_reference,
        title=opp.title or "(untitled)",
        description=opp.description,
        notice_type="opportunity",
        status="active",  # public listing defaults to open opportunities
        buyer_name=opp.buyer_name,
        buyer_country="United Kingdom",
        buyer_region=opp.buyer_region,
        cpv_codes=[],
        keywords=opp.keywords,
        value_amount=opp.value_amount,
        value_currency=opp.value_currency,
        deadline_at=opp.expression_end,
        contract_start=opp.contract_start,
        contract_end=opp.contract_end,
        documents=[],
        raw={
            "advert_id": opp.advert_id,
            "dn_reference": opp.dn_reference,
            "discovered_via": "proactis_public",
            "categories": opp.categories,
            "expression_start": _iso(opp.expression_start),
            "expression_end": _iso(opp.expression_end),
        },
    )
    tender, action = _upsert_tender(db, normalised)
    deduped = action == "new" and tender.duplicate_of_id is not None
    return action, deduped


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


# ---------------------------------------------------------------------------
# Source row + PollRun bookkeeping
# ---------------------------------------------------------------------------


def _ensure_proactis_source(db: Session) -> Source:
    """Get-or-create the PROACTIS ``Source`` row. Proactis isn't in the
    ``ADAPTERS`` registry (it's not an HTTP-poll OCDS source), but a Source row
    gives ``PollRun`` something to FK to."""
    existing = db.execute(
        select(Source).where(Source.code == PROACTIS_SOURCE_CODE)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    source = Source(
        code=PROACTIS_SOURCE_CODE,
        name="Proactis (procontract.due-north.com)",
        base_url=PROACTIS_BASE,
        enabled=True,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


def _finalise_poll_run(db_factory, result: DiscoveryRunResult) -> None:
    """Write the result counters back to the PollRun row."""
    if result.poll_run_id is None:
        return
    with db_factory() as db:
        poll_run = db.get(PollRun, result.poll_run_id)
        if poll_run is None:
            return
        poll_run.finished_at = datetime.now(UTC)
        poll_run.status = result.status
        poll_run.fetched = result.rows_seen
        poll_run.new_count = result.opportunities_inserted
        poll_run.updated_count = result.opportunities_updated
        poll_run.error = result.error
        db.commit()


# Re-export the helpers tests drive directly.
__all__ = [
    "DiscoveredOpportunity",
    "DiscoveryRunResult",
    "PROACTIS_ADVERT_URL",
    "PROACTIS_OPPORTUNITIES_URL",
    "PROACTIS_SOURCE_CODE",
    "build_listing_url",
    "parse_listing_rows",
    "run",
    "run_blocking",
]


# Convenience for the admin endpoint: a sync wrapper so a thread-pool handler
# can fire the coroutine without needing its own event-loop dance.
def run_blocking(config: ProactisFilterConfig) -> DiscoveryRunResult:
    """Synchronous wrapper for ``run``. Used by the admin manual-trigger
    endpoint when it executes the discovery in a background thread."""
    return asyncio.run(run(config=config))
