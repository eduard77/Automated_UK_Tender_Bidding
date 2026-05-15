"""Sell2Wales adapter.

Sell2Wales shares the OCDS API codebase with Public Contracts Scotland; both
expose `/v1/Notices` with `dateFrom=MM-yyyy`, `noticeType`, `outputType`, and
`locale` parameters. We iterate over months between `since` and now.

Note: as of writing, the Sell2Wales list endpoint frequently returns HTTP 500
("Error converting data type nvarchar to float") from their backend SQL layer
even with the canonical parameters from their public docs. We send the
documented request shape and report the upstream failure cleanly via
`had_errors` rather than masking it as success.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import structlog
from dateutil import parser as dtparser

from tender_agent.adapters.base import SourceAdapter
from tender_agent.adapters.pcs import _months_between
from tender_agent.config import settings
from tender_agent.schemas import NormalisedTender
from tender_agent.services.normaliser import ocds_release_to_tender

logger = structlog.get_logger(__name__)

_LOCALE_EN = 2057
_OUTPUT_OCDS = 0


class Sell2WalesAdapter(SourceAdapter):
    code = "S2W"
    name = "Sell2Wales"
    base_url = settings.sell2wales_api_base

    async def fetch_since(self, since: datetime) -> AsyncIterator[NormalisedTender]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        now = datetime.now(UTC)
        url = f"{self.base_url}/Notices"

        for month, year in _months_between(since, now):
            params = {
                "dateFrom": f"{month:02d}-{year:04d}",
                "noticeType": 2,
                "outputType": _OUTPUT_OCDS,
                "locale": _LOCALE_EN,
            }
            try:
                payload = await self._get_json(url, params=params)
            except Exception as exc:
                self.had_errors = True
                logger.error(
                    "s2w.fetch_failed",
                    error=str(exc),
                    url=url,
                    month=params["dateFrom"],
                )
                continue

            releases = payload.get("releases", []) or payload.get("notices", [])
            for release in releases:
                try:
                    release_date = release.get("date")
                    if release_date:
                        rd = dtparser.isoparse(release_date)
                        if rd < since:
                            continue
                    yield ocds_release_to_tender(
                        release,
                        source_code=self.code,
                        source_url_template=(
                            "https://www.sell2wales.gov.wales/search/show/search_view.aspx"
                            "?ID={ocid}"
                        ),
                    )
                except Exception as exc:
                    logger.warning(
                        "s2w.normalise_failed", error=str(exc), ocid=release.get("ocid")
                    )
