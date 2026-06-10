"""Atamis-on-Salesforce adapter.

Atamis e-procurement runs on Salesforce. The Lightning SPA (`/s/Welcome`) is
403 to non-browser clients, but every tenant also exposes a sibling
VISUALFORCE page that is server-rendered and public — captured from the
operator's browser on 2026-06-10 into tests/fixtures/atamis_listing_page.html
(NHS Health Family) / atamis_listing_empty.html (UK Parliament, zero open
rows, same template):

    https://{tenant}.my.salesforce-sites.com/?searchtype=Projects
        &searchStr=&sortStr=Recently+Published&page={n}&filters=&County=

One adapter PARAMETERISED BY TENANT HOST (the EU-Supply pattern): the NHS
"Health Family" tenant (atamis-1928, ~4,900 opportunities), UK Parliament
(atamis-ukparliament) and future tenants are just config entries. Public, no
login, plain httpx.

What the listing gives us (per captured row markup):
  td.col-ref                         -> procurement_ref (Atamis "C449892" —
                                        the cross-source dedup key)
  td.col-title a.result-title        -> title + detail href; its `uid` query
                                        param is the Salesforce record id
                                        -> source_ref
  span.js-dyn[data-label=...]        -> "Contracting Authority" (buyer_name),
                                        "Opens" (published_at, dd/mm/yyyy),
                                        "Response Deadline" (deadline_at,
                                        d/m/yyyy HH:MM),
                                        "Procurement Route" (notice_type;
                                        sometimes empty on real rows)

Pagination: the results bar carries "Page 1 of N" and the rows are sorted
"Recently Published". The on-page Next control is a JSF postback, but every
row/CSV link round-trips the SAME state as GET params (page=, searchStr=,
sortStr=) — so we walk pages by GET `page=n`. NOTE (the one assumption that
could not be live-verified from CI — the site 403s datacenter IPs): only
page=1 was captured; if page=2 ever responds with page 1's content the
identical-uids guard below stops the walk rather than looping. Opens dates
are NOT monotonic under this sort (real page 1 carries 2022 rows
re-published), so we never early-stop on the date — the page cap +
`atamis_max_pages` bounds the walk and `fetch_since` post-filters on Opens.

FilterProfile push-down: NONE of CPV / region / keyword is pushed source-side
— the listing's filter controls are JSF postbacks needing live ViewState (not
reproducible with plain httpx). Same convention as EU-Supply: pull the recent
pages, log the dimensions left to the unified search, and let the operator's
FilterProfile narrow downstream.
"""
from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from tender_agent.adapters.base import SourceAdapter
from tender_agent.config import settings
from tender_agent.schemas import NormalisedTender

logger = structlog.get_logger(__name__)

# Query string captured verbatim from the live page's own row/CSV links — the
# page round-trips this exact state, so we send what it sends.
LISTING_QUERY = (
    "?searchtype=Projects&searchStr=&sortStr=Recently+Published"
    "&page={page}&filters=&County="
)

_TBODY_RE = re.compile(r"<tbody[^>]*>(.*?)</tbody>", re.IGNORECASE | re.DOTALL)
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_REF_CELL_RE = re.compile(
    r'<td[^>]*class="col-ref"[^>]*>(.*?)</td>', re.IGNORECASE | re.DOTALL
)
_TITLE_ANCHOR_RE = re.compile(
    r'<td[^>]*class="col-title"[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_UID_RE = re.compile(r"[?&]uid=([A-Za-z0-9]{15,18})")
_DYN_SPAN_RE = re.compile(
    r'<span[^>]*class="js-dyn"[^>]*data-label="([^"]+)"[^>]*>(.*?)</span>',
    re.IGNORECASE | re.DOTALL,
)
_PAGE_OF_RE = re.compile(r"Page\s+\d+\s+of\s+([\d,]+)", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
# d/m/yyyy or dd/mm/yyyy, optionally followed by HH:MM (the deadline format).
_DATE_RE = re.compile(
    r"(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+(\d{1,2}):(\d{2}))?"
)


def _unescape(text: str) -> str:
    return (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .strip()
    )


def _strip(text: str) -> str:
    return _unescape(re.sub(r"\s+", " ", _TAG_RE.sub(" ", text)))


def _parse_dt(value: str) -> datetime | None:
    m = _DATE_RE.search(value or "")
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hour = int(m.group(4)) if m.group(4) else 0
    minute = int(m.group(5)) if m.group(5) else 0
    try:
        # Atamis shows UK-local wall-clock with no tz; treat as UTC like the
        # other UK-portal adapters do (dates matter, not the exact hour).
        return datetime(year, month, day, hour, minute, tzinfo=UTC)
    except ValueError:
        return None


def _total_pages(html: str) -> int:
    m = _PAGE_OF_RE.search(html)
    if not m:
        return 1
    try:
        return max(0, int(m.group(1).replace(",", "")))
    except ValueError:
        return 1


class AtamisAdapter(SourceAdapter):
    code = "ATAMIS"
    name = "Atamis (Salesforce)"
    base_url = settings.atamis_api_base

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        portals: list[str] | None = None,
        max_pages: int | None = None,
    ) -> None:
        super().__init__(client)
        self._portals = portals if portals is not None else list(
            settings.atamis_portals
        )
        self._max_pages = (
            max_pages if max_pages is not None else settings.atamis_max_pages
        )

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
        wait=wait_exponential_jitter(initial=1, max=30),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def _get_text(self, url: str) -> str:
        logger.debug("source.fetch", adapter=self.code, url=url)
        resp = await self._client.get(
            url, headers={"Accept": "text/html,application/xhtml+xml"}
        )
        resp.raise_for_status()
        return resp.text

    async def fetch_since(
        self, since: datetime
    ) -> AsyncIterator[NormalisedTender]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)

        # FilterProfile dimensions we deliberately do NOT push source-side —
        # the Visualforce filter controls need live JSF ViewState. Logged so
        # the convention is visible in the run's structured output.
        logger.info(
            "atamis.filters_not_pushed",
            dims=["cpv", "region", "keyword"],
            narrowed_by="unified search FilterProfile downstream",
        )

        for host in self._portals:
            host = host.rstrip("/")
            seen_uids: set[str] = set()
            page = 1
            total = 1
            while page <= min(total if total else 1, self._max_pages):
                url = f"{host}/{LISTING_QUERY.format(page=page)}"
                try:
                    html = await self._get_text(url)
                except httpx.HTTPError as exc:
                    self.had_errors = True
                    logger.error(
                        "atamis.fetch_failed", host=host, page=page, error=str(exc)
                    )
                    break

                total = _total_pages(html)
                body = _TBODY_RE.search(html)
                rows = _ROW_RE.findall(body.group(1)) if body else []

                new_on_page = 0
                for row_html in rows:
                    try:
                        tender = self._row_to_tender(row_html, host, since)
                    except _SkipRowError:
                        continue
                    except Exception as exc:  # noqa: BLE001 - one bad row mustn't stop the sweep
                        logger.warning(
                            "atamis.row_parse_failed", host=host, error=str(exc)
                        )
                        continue
                    if tender is None:
                        continue
                    if tender.source_ref in seen_uids:
                        # Identical uid re-served — most likely the GET
                        # pagination didn't advance (see module docstring).
                        continue
                    seen_uids.add(tender.source_ref)
                    new_on_page += 1
                    yield tender

                if rows and new_on_page == 0 and page > 1:
                    # A full page with nothing new = the page param isn't
                    # advancing the listing; stop instead of spinning.
                    logger.warning(
                        "atamis.pagination_stalled", host=host, page=page
                    )
                    break
                if total > self._max_pages and page == self._max_pages:
                    logger.info(
                        "atamis.pages_capped",
                        host=host,
                        total_pages=total,
                        cap=self._max_pages,
                    )
                page += 1

    def _row_to_tender(
        self, row_html: str, host: str, since: datetime
    ) -> NormalisedTender | None:
        anchor = _TITLE_ANCHOR_RE.search(row_html)
        if anchor is None:
            # Header / spacer / "no results" row.
            raise _SkipRowError

        href = _unescape(anchor.group(1))
        uid_match = _UID_RE.search(href)
        if uid_match is None:
            raise _SkipRowError
        uid = uid_match.group(1)

        title = _strip(anchor.group(2)) or "(untitled)"
        source_url = href if href.startswith("http") else f"{host}{href}"

        ref_cell = _REF_CELL_RE.search(row_html)
        reference = _strip(ref_cell.group(1)) if ref_cell else ""

        labels = {
            _strip(label): _strip(value)
            for label, value in _DYN_SPAN_RE.findall(row_html)
        }
        published = _parse_dt(labels.get("Opens", ""))
        if published is not None and published < since:
            # Post-filter only — "Recently Published" ordering is NOT
            # monotonic on Opens (verified on the captured page), so we skip
            # this row but keep walking.
            return None

        return NormalisedTender(
            source_code=self.code,
            source_ref=uid,
            source_url=source_url,
            procurement_ref=reference or None,
            title=title,
            notice_type=labels.get("Procurement Route") or "tender",
            status="active",
            buyer_name=labels.get("Contracting Authority") or None,
            buyer_country="United Kingdom",
            cpv_codes=[],
            keywords=[],
            published_at=published,
            deadline_at=_parse_dt(labels.get("Response Deadline", "")),
            documents=[],
            raw={
                "host": host,
                "uid": uid,
                "reference": reference or None,
                "discovered_via": "atamis_visualforce_listing",
            },
        )


class _SkipRowError(Exception):
    pass
