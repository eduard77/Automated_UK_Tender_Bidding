"""EU-Supply / Mercell CTM adapter.

EU-Supply (footer "Powered by Mercell CTM") powers many UK regional/sector
tender portals — YORtender, the Blue Light police portal, and others. Every
tenant exposes the SAME server-rendered public listing at
``/ctm/Supplier/PublicTenders`` with NO login: an HTML table of the current
OPEN public tenders. One adapter sweeps every configured tenant host.

What the public listing gives us (the dedup-critical + display fields):
  Quote/tender Id  -> source_ref  (EU-Supply's platform-global id)
  Reference        -> procurement_ref  (the buyer's own tender reference)
  Name + link      -> title + source_url (the rfq entrance URL, carries PID)
  Date of publication / Response deadline -> published_at / deadline_at
  Process / Buyers / Countries -> notice_type / buyer_name / buyer_country

Each cell wraps the full value in a ``title="..."`` tooltip attribute (the cell
text is truncated for display), so we read the title attribute when present.

LIMITATION (logged, not blocking): EU-Supply's source-side filtering and
deep pagination are driven by a JS/AJAX POST of the search form (which needs a
live CTM session, not reproducible with plain httpx), so this adapter pulls the
public listing's current page and lets the unified search FilterProfile narrow
by CPV / region / keyword downstream. Region in particular has no public filter
field on EU-Supply at all. See the PR notes.
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

LISTING_PATH = "/ctm/Supplier/PublicTenders"

_TBODY_RE = re.compile(r"<tbody[^>]*>(.*?)</tbody>", re.IGNORECASE | re.DOTALL)
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
_TITLE_ATTR_RE = re.compile(r'title="([^"]*)"', re.IGNORECASE)
_ANCHOR_RE = re.compile(
    r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")
# dd/mm/yyyy optionally followed by HH:MM(:SS)
_DATE_RE = re.compile(
    r"(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)

# Column order of the public-tender table (stable across CTM tenants).
COL_QUOTE_ID = 0
COL_REFERENCE = 1
COL_NAME = 2
COL_PUBLISHED = 3
COL_DEADLINE = 4
COL_PROCESS = 5
COL_BUYER = 6
COL_COUNTRY = 7
_MIN_COLS = 8


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


def _cell_value(cell_html: str) -> str:
    """The full cell value: the tooltip `title` attribute when present (it holds
    the untruncated value), else the visible text."""
    m = _TITLE_ATTR_RE.search(cell_html)
    if m:
        return _unescape(m.group(1))
    return _strip(cell_html)


def _parse_dt(value: str) -> datetime | None:
    m = _DATE_RE.search(value or "")
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hour = int(m.group(4)) if m.group(4) else 0
    minute = int(m.group(5)) if m.group(5) else 0
    second = int(m.group(6)) if m.group(6) else 0
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=UTC)
    except ValueError:
        return None


class EUSupplyAdapter(SourceAdapter):
    code = "EU_SUPPLY"
    name = "EU-Supply / Mercell CTM"
    base_url = settings.eu_supply_api_base

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        portals: list[str] | None = None,
    ) -> None:
        super().__init__(client)
        # Sweep every configured tenant host; default is the primary one.
        self._portals = portals if portals is not None else list(
            settings.eu_supply_portals
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

        for host in self._portals:
            host = host.rstrip("/")
            url = f"{host}{LISTING_PATH}"
            try:
                html = await self._get_text(url)
            except httpx.HTTPError as exc:
                self.had_errors = True
                logger.error("eu_supply.fetch_failed", host=host, error=str(exc))
                continue

            body = _TBODY_RE.search(html)
            rows = _ROW_RE.findall(body.group(1) if body else html)
            for row_html in rows:
                try:
                    tender = self._row_to_tender(row_html, host, since)
                except _SkipRowError:
                    continue
                except Exception as exc:  # noqa: BLE001 - one bad row mustn't stop the sweep
                    logger.warning("eu_supply.row_parse_failed", error=str(exc))
                    continue
                if tender is not None:
                    yield tender

    def _row_to_tender(
        self, row_html: str, host: str, since: datetime
    ) -> NormalisedTender | None:
        cells = _CELL_RE.findall(row_html)
        if len(cells) < _MIN_COLS:
            # Malformed / non-data row (e.g. a "no results" spanning cell).
            raise _SkipRowError

        quote_id = _cell_value(cells[COL_QUOTE_ID])
        if not quote_id:
            raise _SkipRowError

        published = _parse_dt(_cell_value(cells[COL_PUBLISHED]))
        if published is not None and published < since:
            return None

        reference = _cell_value(cells[COL_REFERENCE]) or None

        anchor = _ANCHOR_RE.search(cells[COL_NAME])
        if anchor is not None:
            href = _unescape(anchor.group(1))
            title = _strip(anchor.group(2)) or "(untitled)"
            source_url = href if href.startswith("http") else f"{host}{href}"
        else:
            title = _cell_value(cells[COL_NAME]) or "(untitled)"
            source_url = f"{host}{LISTING_PATH}"

        return NormalisedTender(
            source_code=self.code,
            source_ref=quote_id,
            source_url=source_url,
            procurement_ref=reference,
            title=title,
            notice_type=_cell_value(cells[COL_PROCESS]) or "tender",
            status="active",
            buyer_name=_cell_value(cells[COL_BUYER]) or None,
            buyer_country=_cell_value(cells[COL_COUNTRY]) or None,
            cpv_codes=[],
            keywords=[],
            published_at=published,
            deadline_at=_parse_dt(_cell_value(cells[COL_DEADLINE])),
            documents=[],
            raw={
                "host": host,
                "quote_tender_id": quote_id,
                "reference": reference,
                "discovered_via": "eu_supply_public_listing",
            },
        )


class _SkipRowError(Exception):
    pass
