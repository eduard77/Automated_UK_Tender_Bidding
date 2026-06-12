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
    """Confirm-then-advance watermark over a paged feed.

    The poll watermark normally only advances on a fully-clean run, so a
    source failing on page N re-fetches the ENTIRE window every retry (the
    CF 429 spiral, 2026-06-12: each retry re-pulled a 10-day backlog of 16+
    pages, which itself re-triggered the 429). This tracker lets an adapter
    persist partial progress safely:

    - `page_done(page_min, page_max)` is called after each page's records
      have been fully consumed by the ingester.
    - Page k's max timestamp becomes the watermark only once page k+1
      PROVES the feed ascends (its min >= page k's max). A descending or
      unordered feed therefore never advances mid-run — advancing on a
      newest-first feed would silently skip every older unfetched page.
    - The final page is never confirmed, so a resume re-fetches at most one
      page of overlap; the upsert change-hash makes that idempotent.
    """

    def __init__(self) -> None:
        self._pending_max: datetime | None = None
        self._frozen = False
        self.watermark: datetime | None = None

    def page_done(
        self, page_min: datetime | None, page_max: datetime | None
    ) -> None:
        if self._frozen:
            return
        if self._pending_max is not None and page_min is not None:
            if page_min >= self._pending_max:
                # Ascension confirmed — the previous page is safely behind us.
                self.watermark = self._pending_max
            else:
                # Newest-first or unordered feed: freeze. Never advance on a
                # feed where later pages hold OLDER records.
                self._frozen = True
                return
        if page_max is not None and (
            self._pending_max is None or page_max > self._pending_max
        ):
            self._pending_max = page_max


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
