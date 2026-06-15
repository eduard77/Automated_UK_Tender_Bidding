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

Hardened again after the 2026-06-14 page-14 blockage: salvage rescued the
records on the bad page but the sweep then HALTED there, every poll —
pagination never reached the pages after it, so FTS's watermark froze at the
last clean page (2026-06-02) and never advanced to current tenders. The bad
page is permanent (one persistently-malformed notice upstream), not
transient. A salvaged page no longer ends the sweep:

- the incident body was COMPLETE (the JSON died at char 665438 of 1,053,875
  — garbled mid-array, not truncated at the end), so the package-level
  `links.next` cursor, which sits OUTSIDE the `releases` array at the tail,
  survives. `salvage_next_link` recovers it and the sweep CONTINUES past the
  bad page (`fts.salvaged_continued method=cursor`);
- only if the cursor ITSELF is in the corrupted bytes do we fall back to a
  date-window restart (`updatedFrom` keyed off the newest salvaged record),
  which re-derives a fresh cursor chain WITHOUT depending on the broken
  page's cursor (`fts.salvaged_continued method=date_window`). It requires
  real date progress, so it can never loop on the same blockage;
- a salvaged page that is genuinely the last page ends the sweep cleanly; a
  page we truly cannot advance past logs `fts.salvaged_blocked`. Either way
  the watermark reflects the pages actually confirmed.

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

#: Bounded date-window restarts per run, used ONLY when a salvaged page's
#: `links.next` cursor is itself in the corrupted bytes (so it can't be
#: followed). Each restart must make strict date progress (see fetch_since),
#: so this is a backstop against pathological feeds, not the primary bound —
#: a healthy continue follows the recovered cursor with no restart at all.
MAX_DATE_WINDOW_RESTARTS = 10


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


def salvage_next_link(text: str) -> str | None:
    """Recover the package-level `links.next` cursor from a body whose
    `releases` array failed to parse.

    FTS emits the WHOLE package body (the 2026-06-14 page-14 blockage died at
    char 665438 of 1,053,875 — garbled mid-array, body complete), so the
    `links` object, which sits OUTSIDE the releases array, is almost always
    still intact. Recovering its `next` lets the sweep CONTINUE past the bad
    page instead of halting on it.

    Scans every `"links"` occurrence and `raw_decode`s the object that
    follows, returning the last one carrying a string `next` — that is the
    package-level links (OCDS release objects don't carry a top-level
    links/next, and the package links is emitted at the tail). Returns None
    when `links` is itself unrecoverable; the caller then falls back to
    date-window pagination."""
    decoder = json.JSONDecoder()
    found: str | None = None
    search_from = 0
    while True:
        marker = text.find('"links"', search_from)
        if marker == -1:
            break
        search_from = marker + len('"links"')
        brace = text.find("{", marker)
        if brace == -1:
            continue
        try:
            obj, _ = decoder.raw_decode(text, brace)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            nxt = obj.get("next")
            if isinstance(nxt, str) and nxt:
                found = nxt
    return found


def _body_mentions_next(text: str) -> bool:
    """True when a (garbled) body still shows a `"next"` link key. There IS a
    further page we just couldn't parse the cursor for, so a date-window
    restart is warranted. Absent → the bad page was the LAST page; the sweep
    ends cleanly rather than restarting into nothing."""
    return '"next"' in text


def _page_dates(releases: list[dict]) -> list[datetime]:
    """The releases' parsed `date` fields in document order, undated/unparseable
    releases omitted. Document order matters: the progress tracker reads
    intra-page direction from the first page's record order."""
    dates: list[datetime] = []
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
        dates.append(stamp)
    return dates


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
        base_url = f"{self.base_url}/ocdsReleasePackages"
        params = {"updatedFrom": since.strftime("%Y-%m-%dT%H:%M:%SZ")}
        url: str | None = base_url
        # Send `params` (the `updatedFrom` window) on the next fetch. True for
        # page 1 and after every date-window restart; False while following a
        # `links.next` cursor, which already encodes the window.
        send_params = True
        page = 0
        tracker = PageProgressTracker()
        date_window_restarts = 0
        date_window_floor: datetime | None = None
        # Cursor URLs already followed THIS chain — guards an A→B→A cursor
        # cycle on a misbehaving feed. Cleared on a date-window restart (which
        # starts a fresh chain from a strictly higher floor).
        seen_cursors: set[str] = set()
        while url:
            page += 1
            try:
                text = await self._get_page_text(
                    url, params=params if send_params else None
                )
            except Exception as exc:  # noqa: BLE001
                self.had_errors = True
                self.record_error(f"page {page}: {type(exc).__name__}: {exc}")
                logger.error("fts.fetch_failed", error=str(exc), url=url)
                break
            send_params = False

            payload: dict | None = None
            releases: list[dict]
            salvaged = False
            parse_position = 0
            skipped = 0
            try:
                payload = json.loads(text)
                releases = payload.get("releases", [])
            except json.JSONDecodeError as exc:
                # A garbled/truncated page: recover every complete release
                # rather than losing the ~1000 already-transferred records
                # (the 2026-06-12 failure shape). One bad notice fails alone.
                releases, skipped = salvage_releases(text)
                parse_position = exc.pos
                salvaged = True
                self.had_errors = True
                logger.error(
                    "fts.page_salvaged",
                    page=page,
                    error_position=parse_position,
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
            # by the time control returns here — safe to confirm progress. The
            # watermark is set just before the next page's network fetch (the
            # await where a 900s-timeout cancellation lands); poll_source's
            # cancellation-persist (except CancelledError) commits it if the
            # cancel hits mid-fetch.
            page_dates = _page_dates(releases)
            page_max = max(page_dates) if page_dates else None
            tracker.page_done(page_dates)
            self.progress_watermark = tracker.watermark

            if not salvaged:
                links = (payload.get("links") if payload else None) or {}
                next_url = links.get("next")
                if next_url and next_url != url and next_url not in seen_cursors:
                    seen_cursors.add(next_url)
                    url = next_url
                else:
                    url = None
                continue

            # --- salvaged page: CONTINUE past it, never halt here ----------
            # (2026-06-14 page-14 blockage: the old `break` meant the sweep
            # never advanced past a permanently-bad page and the watermark
            # froze.) The salvage already set had_errors; record the
            # disposition alongside it so the PollRun shows we progressed.
            diag = (
                f"page {page}: malformed JSON at char {parse_position} of "
                f"{len(text)} — salvaged {len(releases)} releases "
                f"({skipped} skipped regions)"
            )

            # (1) The body is usually complete (garbled mid-array), so the
            #     package `links.next` cursor at the tail survives — follow it.
            next_url = salvage_next_link(text)
            if next_url and next_url != url and next_url not in seen_cursors:
                seen_cursors.add(next_url)
                url = next_url
                self.record_error(f"{diag}; recovered next cursor — CONTINUING")
                logger.warning(
                    "fts.salvaged_continued",
                    page=page,
                    method="cursor",
                    salvaged=len(releases),
                    skipped_regions=skipped,
                )
                continue

            # (2) Cursor unrecoverable but the body shows there IS a further
            #     page → restart a fresh cursor chain from a date window past
            #     the newest salvaged record. Requires strict date progress,
            #     so it can never loop on the same blockage.
            if (
                _body_mentions_next(text)
                and page_max is not None
                and date_window_restarts < MAX_DATE_WINDOW_RESTARTS
                and (date_window_floor is None or page_max > date_window_floor)
            ):
                date_window_floor = page_max
                date_window_restarts += 1
                stamp = page_max.strftime("%Y-%m-%dT%H:%M:%SZ")
                params = {"updatedFrom": stamp}
                url = base_url
                send_params = True
                seen_cursors.clear()
                self.record_error(
                    f"{diag}; cursor unrecoverable — RESTARTING from "
                    f"updatedFrom={stamp}"
                )
                logger.warning(
                    "fts.salvaged_continued",
                    page=page,
                    method="date_window",
                    restart=date_window_restarts,
                    updated_from=stamp,
                    salvaged=len(releases),
                )
                continue

            # (3) No further page (genuinely the last page), or we cannot
            #     advance past it. Either way the confirmed watermark stands;
            #     stop. `salvaged_blocked` is the case worth alarming on.
            if _body_mentions_next(text):
                self.record_error(
                    f"{diag}; cannot advance past it (no recoverable cursor, "
                    f"no further date progress) — pagination stops here"
                )
                logger.error(
                    "fts.salvaged_blocked",
                    page=page,
                    salvaged=len(releases),
                    skipped_regions=skipped,
                )
            else:
                self.record_error(f"{diag}; no further pages — end of feed")
                logger.info(
                    "fts.salvaged_end_of_feed",
                    page=page,
                    salvaged=len(releases),
                )
            url = None
