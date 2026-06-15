"""Base adapter for tender publication sources.

Each source has its own adapter that knows how to:
1. Page through that source's listing/feed/API since a given timestamp
2. Yield raw payloads for each tender notice
3. Convert a raw payload to a NormalisedTender

This keeps source-specific quirks isolated.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime  # noqa: TC003 — used at runtime by the tracker

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from tender_agent.config import settings
from tender_agent.schemas import NormalisedTender

logger = structlog.get_logger(__name__)


class TruncatedResponseError(httpx.TransportError):
    """The response body was shorter than its declared Content-Length —
    the payload was cut mid-stream (proxy/idle-timeout truncation). Subclasses
    httpx.TransportError so the standard retry policy treats it as a
    transient network fault."""


class PageProgressTracker:
    """Direction-aware resume watermark over a paged feed.

    The resume watermark must advance for EVERY page whose records have been
    committed — otherwise a backlog catch-up that the 900s poll timeout
    cancels mid-sweep keeps no progress and the next run restarts from the
    backlog start, times out again, and never drains (the CF/FTS 2026-06-14
    non-advancing loop). But it must NEVER move onto a date whose still-
    unfetched records would then be skipped. Those two pulls are reconciled by
    the feed's sort direction:

    - On an ASCENDING (oldest-first) feed, once pages 1..k have been consumed
      in order every record dated <= the max date seen is on a consumed page,
      so resuming from that max skips nothing (the >= overlap re-fetches at
      most the boundary, which the upsert content-hash makes idempotent). The
      watermark therefore advances to the running max after EACH consumed
      page — including the first page and a single-page window, so a cancelled
      run keeps the progress of every committed page.
    - On a DESCENDING / unordered feed, advancing would skip the older, still-
      unfetched pages, so the tracker FREEZES and never advances.

    Direction is read from the strongest signal available:

    - A later page whose records START at/after the previous page's max proves
      the feed ascends globally (authoritative); one that starts BEFORE proves
      it descends/unordered → freeze.
    - Before any cross-page signal exists (i.e. on the first page), the page's
      OWN record order is a provisional read. CF and FTS are cursor-harvested
      OCDS ``updatedFrom`` feeds, globally sorted by update time, so they order
      records the same within a page as across pages — an ascending first page
      means an ascending feed. This is what lets a run cancelled ON PAGE 1 keep
      page 1's progress instead of discarding it.
    - A page with fewer than two dated records carries no intra-page signal and
      leaves the direction UNKNOWN until a cross-page signal arrives; the
      watermark holds (stays put) until then rather than guessing.

    The watermark is monotonic — it never moves backwards.
    """

    def __init__(self) -> None:
        self.watermark: datetime | None = None
        self._running_max: datetime | None = None
        self._prev_page_max: datetime | None = None
        self._ascending = False
        self._frozen = False

    def page_done(self, page_dates: list[datetime] | None) -> None:
        """Confirm a page whose records have been fully consumed.

        ``page_dates`` are the records' ``date`` values in the order the feed
        delivered them (document order); the caller omits undated records.
        Passing them in order (rather than a pre-reduced min/max) is what lets
        the tracker read intra-page direction on the first page.
        """
        if self._frozen:
            return
        dates = [d for d in (page_dates or []) if d is not None]
        if not dates:
            return  # no dated records — can't reason about progress; hold
        page_min = min(dates)
        page_max = max(dates)

        if self._prev_page_max is not None:
            # Cross-page signal — authoritative over any intra-page read.
            if page_min >= self._prev_page_max:
                self._ascending = True
            else:
                # Later page holds OLDER records: descending/unordered. Freeze
                # before advancing — moving forward would skip those pages.
                self._frozen = True
                return
        elif len(dates) >= 2:
            # First page, provisional intra-page read (globally-sorted feed).
            if dates[-1] > dates[0]:
                self._ascending = True
            elif dates[-1] < dates[0]:
                self._frozen = True
                return

        if self._running_max is None or page_max > self._running_max:
            self._running_max = page_max
        if self._ascending:
            self.watermark = self._running_max
        self._prev_page_max = page_max


class SourceAdapter(ABC):
    """Subclass per tender source."""

    code: str
    name: str
    base_url: str

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            # Generous per-read budget for multi-MB page bodies (FTS); the
            # tighter default still bounds connect/write/pool.
            timeout=httpx.Timeout(
                settings.http_timeout_seconds,
                read=settings.http_read_timeout_seconds,
            ),
            headers={"User-Agent": settings.http_user_agent, "Accept": "application/json"},
            follow_redirects=True,
        )
        # Set to True by a subclass when an HTTP request fails permanently so
        # ingestion can mark the poll run as status="error" while still
        # persisting any records that were yielded before the failure.
        self.had_errors = False
        # NEW (Phase-1 continuation, 2026-06-11): adapters APPEND a short
        # human-readable message to this list every time they set had_errors,
        # so `poll_source` can surface the REAL upstream cause (a 403 vs a
        # 500 vs DNS) on the PollRun.error column instead of the legacy
        # generic "upstream HTTP requests failed" — which the sources-health
        # endpoint then displays verbatim. Bounded length so a flapping
        # source can't blow the row.
        self.error_messages: list[str] = []
        # Partial-progress watermark (2026-06-12). Paging adapters set this
        # (via PageProgressTracker) as pages complete; `poll_source` persists
        # it on a FAILED run so retries resume from the last confirmed page
        # instead of re-fetching the whole window. None = no safe progress.
        self.progress_watermark: datetime | None = None

    @staticmethod
    def check_body_complete(response: httpx.Response) -> None:
        """Raise TruncatedResponseError when the body is shorter than its
        declared Content-Length (only checkable on identity encoding — for
        compressed bodies the header counts compressed bytes)."""
        encoding = (response.headers.get("Content-Encoding") or "identity").lower()
        declared = response.headers.get("Content-Length")
        if encoding not in ("", "identity") or not (declared and declared.isdigit()):
            return
        actual = len(response.content)
        if actual < int(declared):
            raise TruncatedResponseError(
                f"response truncated: got {actual} of {declared} declared bytes"
            )

    def record_error(self, message: str, limit: int = 5) -> None:
        """Append a short upstream-error description for the PollRun row.

        Adapters call this alongside ``self.had_errors = True``. Long
        exception reprs are truncated so a SQL stacktrace never lands in
        the column; we keep the latest few so a multi-host sweep
        (EU-Supply, Atamis) doesn't drop the first failure on the floor."""
        text = (message or "").strip()
        if not text:
            return
        # Single-line + truncated — the column is Text but the operator's
        # reading it as a tooltip, not a dump.
        text = " ".join(text.split())
        if len(text) > 320:
            text = text[:317] + "..."
        self.error_messages.append(text)
        if len(self.error_messages) > limit:
            self.error_messages = self.error_messages[-limit:]

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> SourceAdapter:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
        wait=wait_exponential_jitter(initial=1, max=30),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def _get_json(self, url: str, params: dict | None = None) -> dict:
        logger.debug("source.fetch", adapter=self.code, url=url, params=params)
        response = await self._client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    @abstractmethod
    async def fetch_since(self, since: datetime) -> AsyncIterator[NormalisedTender]:
        """Yield normalised tenders published or updated since `since`."""
        ...
        if False:  # pragma: no cover - typing aid for AsyncIterator
            yield  # type: ignore[unreachable]
