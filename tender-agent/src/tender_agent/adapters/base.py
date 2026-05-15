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
