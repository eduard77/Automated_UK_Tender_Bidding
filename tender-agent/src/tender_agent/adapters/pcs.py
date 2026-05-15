"""Public Contracts Scotland adapter.

PCS publishes OCDS release packages via the same Sell2Wales codebase. The
endpoint is month-granular: pass `dateFrom=MM-yyyy` and the API returns every
notice published in that month. We iterate over each month between `since` and
now.

The `api.publiccontractsscotland.gov.uk` host serves only its leaf certificate
without the Sectigo intermediate, so we bundle the intermediate in
`extra_ca/` and build a custom SSL context that trusts it. The intermediate is
a public CA cert downloaded from the AIA URL in the leaf cert
(http://crt.sectigo.com/SectigoPublicServerAuthenticationCADVR36.crt).

Reference: https://www.publiccontractsscotland.gov.uk/Search/Search_AuthApi.aspx
"""
from __future__ import annotations

import ssl
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import certifi
import httpx
import structlog

from tender_agent.adapters.base import SourceAdapter
from tender_agent.config import settings
from tender_agent.schemas import NormalisedTender
from tender_agent.services.normaliser import ocds_release_to_tender

logger = structlog.get_logger(__name__)

_EXTRA_CA = (
    Path(__file__).parent / "extra_ca" / "sectigo_public_server_auth_ca_dv_r36.pem"
)

_LOCALE_EN = 2057
_OUTPUT_OCDS = 0


def _build_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=certifi.where())
    if _EXTRA_CA.exists():
        ctx.load_verify_locations(cafile=str(_EXTRA_CA))
    return ctx


def _months_between(start: datetime, end: datetime) -> list[tuple[int, int]]:
    """Return inclusive list of (month, year) tuples from start to end."""
    months: list[tuple[int, int]] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append((m, y))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months


class PCSAdapter(SourceAdapter):
    code = "PCS"
    name = "Public Contracts Scotland"
    base_url = settings.pcs_api_base

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        if client is None:
            client = httpx.AsyncClient(
                timeout=settings.http_timeout_seconds,
                headers={
                    "User-Agent": settings.http_user_agent,
                    "Accept": "application/json",
                },
                follow_redirects=True,
                verify=_build_ssl_context(),
            )
        super().__init__(client=client)

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
                    "pcs.fetch_failed",
                    error=str(exc),
                    url=url,
                    month=params["dateFrom"],
                )
                continue

            releases = payload.get("releases", []) or payload.get("notices", [])
            for release in releases:
                # Post-filter to records updated/published since the cutoff so we
                # don't re-emit the whole month each poll.
                try:
                    release_date = release.get("date")
                    if release_date:
                        from dateutil import parser as dtparser

                        rd = dtparser.isoparse(release_date)
                        if rd < since:
                            continue
                    yield ocds_release_to_tender(
                        release,
                        source_code=self.code,
                        source_url_template=(
                            "https://www.publiccontractsscotland.gov.uk/search/show/"
                            "search_view.aspx?ID={ocid}"
                        ),
                    )
                except Exception as exc:
                    logger.warning(
                        "pcs.normalise_failed",
                        error=str(exc),
                        ocid=release.get("ocid"),
                    )
