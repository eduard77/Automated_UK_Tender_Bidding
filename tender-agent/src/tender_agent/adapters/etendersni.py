"""eTendersNI adapter.

eTendersNI doesn't publish full OCDS feeds publicly. We parse their Atom feed
and produce minimally-populated NormalisedTender records, then enrich on demand
when the document downloader visits the notice page.

If your subscription provides API access (e.g. eSenders), this adapter can be
swapped for a richer one without changing the rest of the pipeline.
"""
from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from xml.etree import ElementTree as ET

import httpx
import structlog
from dateutil import parser as dtparser

from tender_agent.adapters.base import SourceAdapter
from tender_agent.config import settings
from tender_agent.schemas import NormalisedTender

logger = structlog.get_logger(__name__)

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
NOTICE_ID_RE = re.compile(r"(?:notice|tender)[\W_]+id[\W_]+([A-Z0-9-]+)", re.IGNORECASE)


class ETendersNIAdapter(SourceAdapter):
    code = "NI"
    name = "eTendersNI"
    base_url = settings.etendersni_feed_url

    async def fetch_since(self, since: datetime) -> AsyncIterator[NormalisedTender]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)

        try:
            resp = await self._client.get(
                self.base_url,
                headers={"Accept": "application/atom+xml, application/xml, text/xml"},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            self.had_errors = True
            self.record_error(f"fetch failed: {type(exc).__name__}: {exc}")
            logger.error("ni.fetch_failed", error=str(exc), url=self.base_url)
            return

        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as exc:
            self.had_errors = True
            self.record_error(f"parse failed: {exc}")
            logger.error("ni.parse_failed", error=str(exc))
            return

        for entry in root.findall("atom:entry", ATOM_NS):
            try:
                yield self._entry_to_tender(entry, since)
            except _SkipEntryError:
                continue
            except Exception as exc:
                logger.warning("ni.normalise_failed", error=str(exc))

    def _entry_to_tender(self, entry: ET.Element, since: datetime) -> NormalisedTender:
        title_el = entry.find("atom:title", ATOM_NS)
        link_el = entry.find("atom:link", ATOM_NS)
        updated_el = entry.find("atom:updated", ATOM_NS)
        summary_el = entry.find("atom:summary", ATOM_NS)
        author_el = entry.find("atom:author/atom:name", ATOM_NS)

        if title_el is None or link_el is None:
            raise _SkipEntryError

        url = link_el.attrib.get("href", "")
        title = (title_el.text or "").strip() or "(untitled)"
        updated_text = updated_el.text if updated_el is not None and updated_el.text else None
        updated = dtparser.isoparse(updated_text) if updated_text else None
        if updated and updated < since:
            raise _SkipEntryError

        m = NOTICE_ID_RE.search(url) or NOTICE_ID_RE.search(title)
        source_ref = m.group(1) if m else url

        return NormalisedTender(
            source_code=self.code,
            source_ref=source_ref,
            source_url=url,
            procurement_ref=None,
            title=title,
            description=(summary_el.text or "").strip() if summary_el is not None else None,
            notice_type="tender",
            status="active",
            buyer_name=(author_el.text or "").strip() if author_el is not None else None,
            buyer_country="Northern Ireland",
            cpv_codes=[],
            keywords=[],
            published_at=updated or datetime.now(UTC),
            documents=[],
            raw={"atom_entry_url": url},
        )


class _SkipEntryError(Exception):
    pass
