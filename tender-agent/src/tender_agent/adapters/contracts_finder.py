"""Contracts Finder adapter.

Contracts Finder publishes OCDS release packages with cursor-style paging.

Reference: https://www.contractsfinder.service.gov.uk/apidocumentation
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import structlog

from tender_agent.adapters.base import SourceAdapter
from tender_agent.config import settings
from tender_agent.schemas import NormalisedTender
from tender_agent.services.normaliser import ocds_release_to_tender

logger = structlog.get_logger(__name__)


class ContractsFinderAdapter(SourceAdapter):
    code = "CF"
    name = "Contracts Finder"
    base_url = settings.contracts_finder_api_base

    async def fetch_since(self, since: datetime) -> AsyncIterator[NormalisedTender]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        params = {
            "updatedFrom": since.strftime("%Y-%m-%dT%H:%M:%S"),
            "limit": 100,
        }
        url = f"{self.base_url}/Search"
        page = 0

        while url:
            page += 1
            try:
                payload = await self._get_json(url, params=params if page == 1 else None)
            except Exception as exc:
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
                except Exception as exc:
                    logger.warning(
                        "cf.normalise_failed",
                        error=str(exc),
                        ocid=release.get("ocid"),
                    )

            links = payload.get("links") or {}
            next_url = links.get("next")
            url = next_url if next_url and next_url != url else None
