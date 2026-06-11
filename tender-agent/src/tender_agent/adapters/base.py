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
from datetime import datetime

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


class SourceAdapter(ABC):
    """Subclass per tender source."""

    code: str
    name: str
    base_url: str

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
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
