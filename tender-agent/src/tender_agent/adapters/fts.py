"""Find a Tender Service (FTS) adapter.

FTS publishes notices in Open Contracting Data Standard (OCDS) format via
release packages. Each release represents a snapshot of a notice at a point in
time. We page by `updated-from`/`cursor` and yield normalised tenders.

Reference: https://www.find-tender.service.gov.uk/apidocumentation
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


class FTSAdapter(SourceAdapter):
    code = "FTS"
    name = "Find a Tender Service"
    base_url = settings.fts_api_base

    async def fetch_since(self, since: datetime) -> AsyncIterator[NormalisedTender]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        params = {"updated-from": since.strftime("%Y-%m-%dT%H:%M:%S")}
        url = f"{self.base_url}/ocdsReleasePackages"
        page = 0

        while url:
            page += 1
            try:
                payload = await self._get_json(url, params=params if page == 1 else None)
            except Exception as exc:
                logger.error("fts.fetch_failed", error=str(exc), url=url)
                break

            releases = payload.get("releases", [])
            for release in releases:
                try:
                    yield ocds_release_to_tender(
                        release,
                        source_code=self.code,
                        source_url_template="https://www.find-tender.service.gov.uk/Notice/{ocid}",
                    )
                except Exception as exc:
                    logger.warning(
                        "fts.normalise_failed",
                        error=str(exc),
                        ocid=release.get("ocid"),
                    )

            links = payload.get("links") or {}
            next_url = links.get("next")
            url = next_url if next_url and next_url != url else None
