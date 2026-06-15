"""Contracts Finder adapter.

Contracts Finder publishes OCDS release packages with cursor-style paging.

Hardened after the 2026-06-12 CF 429 spiral: every run since June 2 had
failed with 429 Too Many Requests, and because the watermark only advanced
on FULL success, each retry re-fetched the entire 10-day backlog (16+
pages) — which itself re-triggered the 429. Now:

- 429s are RESPECTED: Retry-After is honoured when present, exponential
  backoff otherwise, a bounded number of attempts per page, and the run
  aborts GRACEFULLY (keeping progress) instead of hammering.
- Pages are paced with a small inter-page delay.
- The watermark advances INCREMENTALLY as pages are confirmed processed
  (see PageProgressTracker), so partial progress is kept and each retry's
  window shrinks instead of replaying the whole backlog.

Reference: https://www.contractsfinder.service.gov.uk/apidocumentation
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import structlog
from dateutil import parser as dtparser

from tender_agent.adapters.base import PageProgressTracker, SourceAdapter
from tender_agent.config import settings
from tender_agent.schemas import NormalisedTender
from tender_agent.services.normaliser import ocds_release_to_tender

logger = structlog.get_logger(__name__)

#: Pause between successive page fetches — CF rate-limits aggressively and
#: a 16-page backlog fetched back-to-back is exactly what tripped the 429s.
INTER_PAGE_DELAY_S = 1.0
#: How many times one page retries a 429 before the run aborts gracefully.
RATE_LIMIT_MAX_RETRIES = 3
#: Backoff base when the 429 carries no Retry-After header (2^attempt × base).
RATE_LIMIT_BACKOFF_BASE_S = 10.0
#: Never sleep longer than this on a single Retry-After/backoff wait.
RATE_LIMIT_MAX_WAIT_S = 120.0
#: Quick retries for ordinary transport blips (timeouts, resets) per page.
TRANSIENT_MAX_RETRIES = 2


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse a Retry-After header (delta-seconds or HTTP-date), if present."""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return float(raw)
    try:
        when = dtparser.parse(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - datetime.now(UTC)).total_seconds())
    except (ValueError, OverflowError):
        return None


def _page_dates(releases: list[dict]) -> list[datetime]:
    """The releases' parsed `date` fields, in document order, for the progress
    tracker (undated/unparseable releases are omitted). Document order matters:
    the tracker reads intra-page direction from the first page's record order."""
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


class RateLimitedError(Exception):
    """Raised when a page is still 429 after the bounded retry budget —
    the run aborts gracefully, keeping the progress watermark."""


class ContractsFinderAdapter(SourceAdapter):
    code = "CF"
    name = "Contracts Finder"
    base_url = settings.contracts_finder_api_base

    #: Injectable for tests (so backoff waits don't slow the suite).
    _sleep = staticmethod(asyncio.sleep)

    async def _get_page(self, url: str, params: dict | None) -> dict:
        """Fetch one page, honouring 429s: Retry-After when present,
        exponential backoff otherwise, bounded attempts.

        Deliberately does NOT go through `_get_json`: its blanket tenacity
        policy blind-retries 429s with short waits — the exact hammering
        that kept CF rate-limited. Transport blips get their own quick
        bounded retries; non-429 status errors raise to the page-level
        handler."""
        rate_limit_attempts = 0
        transient_attempts = 0
        while True:
            try:
                response = await self._client.get(url, params=params)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 429:
                    raise
                rate_limit_attempts += 1
                if rate_limit_attempts > RATE_LIMIT_MAX_RETRIES:
                    raise RateLimitedError(
                        f"429 after {rate_limit_attempts} attempts "
                        "(Retry-After honoured); aborting run, progress kept"
                    ) from exc
                wait = _retry_after_seconds(exc.response)
                if wait is None:
                    wait = RATE_LIMIT_BACKOFF_BASE_S * (
                        2 ** (rate_limit_attempts - 1)
                    )
                wait = min(wait, RATE_LIMIT_MAX_WAIT_S)
                logger.warning(
                    "cf.rate_limited",
                    attempt=rate_limit_attempts,
                    wait_seconds=wait,
                    retry_after_header=exc.response.headers.get("Retry-After"),
                )
                await self._sleep(wait)
            except (httpx.TimeoutException, httpx.TransportError):
                transient_attempts += 1
                if transient_attempts > TRANSIENT_MAX_RETRIES:
                    raise
                await self._sleep(2.0 * transient_attempts)

    async def fetch_since(self, since: datetime) -> AsyncIterator[NormalisedTender]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        params = {
            "updatedFrom": since.strftime("%Y-%m-%dT%H:%M:%S"),
            "limit": 100,
        }
        url = f"{self.base_url}/Search"
        page = 0
        tracker = PageProgressTracker()

        while url:
            page += 1
            if page > 1:
                # Pace the backlog: back-to-back page pulls are what tripped
                # CF's rate limiter in the first place.
                await self._sleep(INTER_PAGE_DELAY_S)
            try:
                payload = await self._get_page(url, params if page == 1 else None)
            except RateLimitedError as exc:
                # Graceful abort: everything confirmed so far stays via the
                # progress watermark; the next run's window starts there.
                self.had_errors = True
                self.record_error(f"page {page}: {exc}")
                logger.error(
                    "cf.rate_limit_abort",
                    page=page,
                    progress_watermark=(
                        self.progress_watermark.isoformat()
                        if self.progress_watermark
                        else None
                    ),
                )
                break
            except Exception as exc:  # noqa: BLE001
                self.had_errors = True
                self.record_error(f"page {page}: {type(exc).__name__}: {exc}")
                logger.error("cf.fetch_failed", error=str(exc), url=url)
                break

            releases = payload.get("releases", []) or payload.get("results", [])
            for release in releases:
                try:
                    yield ocds_release_to_tender(
                        release,
                        source_code=self.code,
                        source_url_template="https://www.contractsfinder.service.gov.uk/Notice/{ocid}",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "cf.normalise_failed",
                        error=str(exc),
                        ocid=release.get("ocid"),
                    )

            # Control only returns here after the consumer has processed
            # every yielded release of this page — safe to mark it done. The
            # watermark is set HERE, just before the next page's network fetch
            # (the await where a 900s-timeout cancellation lands); poll_source's
            # cancellation-persist (except CancelledError) commits this
            # value if the cancel hits mid-fetch.
            tracker.page_done(_page_dates(releases))
            self.progress_watermark = tracker.watermark

            links = payload.get("links") or {}
            next_url = links.get("next")
            url = next_url if next_url and next_url != url else None
