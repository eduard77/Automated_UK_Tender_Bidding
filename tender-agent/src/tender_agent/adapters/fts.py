"""Find a Tender Service (FTS) adapter.

FTS publishes notices in Open Contracting Data Standard (OCDS) format via
release packages. Each release represents a snapshot of a notice at a point in
time. We page by `updated-from`/`cursor` and yield normalised tenders.

Hardened after the 2026-06-12 outage: every FTS run was dying on a
JSONDecodeError deep inside multi-MB page bodies, losing the ~1000 records
already parsed AND the watermark. Now:

- the page fetch verifies the body against its declared Content-Length
  (a short body raises TruncatedResponseError — retried like any transport
  fault — and the diagnosis names truncation explicitly);
- an unparseable page is SALVAGED record-by-record: every complete release
  object in the (truncated or garbled) `releases` array is recovered and
  yielded; a malformed record fails ALONE — it is skipped with its position
  logged, the scan resynchronises, and the rest of the page continues;
- the watermark advances incrementally for confirmed pages (see
  PageProgressTracker), so what succeeded stays succeeded even when a later
  page fails.

Reference: https://www.find-tender.service.gov.uk/apidocumentation
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import structlog
from dateutil import parser as dtparser
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from tender_agent.adapters.base import PageProgressTracker, SourceAdapter
from tender_agent.config import settings
from tender_agent.schemas import NormalisedTender
from tender_agent.services.normaliser import ocds_release_to_tender

logger = structlog.get_logger(__name__)

#: How many resynchronisation attempts the salvage scan makes after hitting
#: undecodable bytes — each skips forward to the next '{' and tries again,
#: so one malformed record in the middle doesn't cost the records after it.
SALVAGE_RESYNC_LIMIT = 50


def salvage_releases(text: str) -> tuple[list[dict], int]:
    """Recover complete release objects from a malformed/truncated package.

    Scans the `"releases": [...]` array with raw_decode, collecting every
    complete object. On undecodable bytes it resynchronises at the next
    '{' (bounded by SALVAGE_RESYNC_LIMIT) so records AFTER a malformed one
    are recovered too. Returns (releases, skipped_regions)."""
    marker = text.find('"releases"')
    if marker == -1:
        return [], 0
    start = text.find("[", marker)
    if start == -1:
        return [], 0
    decoder = json.JSONDecoder()
    releases: list[dict] = []
    skipped = 0
    index = start + 1
    while index < len(text):
        while index < len(text) and text[index] in " \t\r\n,":
            index += 1
        if index >= len(text) or text[index] == "]":
            break
        try:
            obj, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            if skipped >= SALVAGE_RESYNC_LIMIT:
                break
            next_obj = text.find("{", index + 1)
            if next_obj == -1:
                break
            skipped += 1
            index = next_obj
            continue
        if isinstance(obj, dict):
            releases.append(obj)
        index = end
    return releases, skipped


def _page_date_bounds(
    releases: list[dict],
) -> tuple[datetime | None, datetime | None]:
    page_min: datetime | None = None
    page_max: datetime | None = None
    for release in releases:
        raw = release.get("date")
        if not raw:
            continue
        try:
            stamp = dtparser.isoparse(raw)
        except (ValueError, TypeError):
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        if page_min is None or stamp < page_min:
            page_min = stamp
        if page_max is None or stamp > page_max:
            page_max = stamp
    return page_min, page_max


class FTSAdapter(SourceAdapter):
    code = "FTS"
    name = "Find a Tender Service"
    base_url = settings.fts_api_base

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
        wait=wait_exponential_jitter(initial=1, max=30),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def _get_page_text(self, url: str, params: dict | None = None) -> str:
        """Fetch one page as TEXT (so a parse failure can be salvaged), with
        the body verified against its declared Content-Length — a truncated
        body is a transport fault and retried, not parsed."""
        logger.debug("source.fetch", adapter=self.code, url=url, params=params)
        response = await self._client.get(url, params=params)
        response.raise_for_status()
        self.check_body_complete(response)
        return response.text

    async def fetch_since(self, since: datetime) -> AsyncIterator[NormalisedTender]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        params = {"updatedFrom": since.strftime("%Y-%m-%dT%H:%M:%SZ")}
        url = f"{self.base_url}/ocdsReleasePackages"
        page = 0
        tracker = PageProgressTracker()
        while url:
            page += 1
            try:
                text = await self._get_page_text(
                    url, params=params if page == 1 else None
                )
            except Exception as exc:  # noqa: BLE001
                self.had_errors = True
                self.record_error(f"page {page}: {type(exc).__name__}: {exc}")
                logger.error("fts.fetch_failed", error=str(exc), url=url)
                break

            payload: dict | None = None
            releases: list[dict]
            salvaged = False
            try:
                payload = json.loads(text)
                releases = payload.get("releases", [])
            except json.JSONDecodeError as exc:
                # A garbled/truncated page: recover every complete release
                # rather than losing the ~1000 already-transferred records
                # (the 2026-06-12 failure shape). One bad notice fails alone.
                releases, skipped = salvage_releases(text)
                salvaged = True
                self.had_errors = True
                self.record_error(
                    f"page {page}: malformed JSON at char {exc.pos} of "
                    f"{len(text)} — salvaged {len(releases)} releases "
                    f"({skipped} skipped regions); pagination stops here"
                )
                logger.error(
                    "fts.page_salvaged",
                    page=page,
                    error_position=exc.pos,
                    body_chars=len(text),
                    salvaged=len(releases),
                    skipped_regions=skipped,
                )

            for release in releases:
                try:
                    yield ocds_release_to_tender(
                        release,
                        source_code=self.code,
                        source_url_template="https://www.find-tender.service.gov.uk/Notice/{ocid}",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "fts.normalise_failed",
                        error=str(exc),
                        ocid=release.get("ocid"),
                    )

            # The consumer has processed every yielded release of this page
            # by the time control returns here — safe to confirm progress.
            tracker.page_done(*_page_date_bounds(releases))
            self.progress_watermark = tracker.watermark

            if salvaged:
                # A salvaged page has no trustworthy `links.next`; stop —
                # the advanced watermark makes the next run resume from here.
                break
            links = (payload.get("links") if payload else None) or {}
            next_url = links.get("next")
            url = next_url if next_url and next_url != url else None
