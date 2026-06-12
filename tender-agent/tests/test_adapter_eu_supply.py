"""Fixture-based tests for the EU-Supply / Mercell CTM adapter.

EU-Supply parses the server-rendered public-tender HTML table at
``/ctm/Supplier/PublicTenders`` (no login) and post-filters by publication date
against the `since` cutoff. One adapter sweeps every configured tenant host.
All HTTP is mocked via httpx.MockTransport — no live network.
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx

from tender_agent.adapters.eu_supply import EUSupplyAdapter
from tender_agent.schemas import NormalisedTender

from .conftest import collect, load_text_fixture, static_text_handler

FIXTURE = "eu_supply_publictenders.html"

# A single-tenant adapter for the default-host tests.
_ONE_PORTAL = ["https://yortender.eu-supply.com"]


def _adapter(handler, portals=None) -> EUSupplyAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return EUSupplyAdapter(client=client, portals=portals or _ONE_PORTAL)


async def test_fetch_since_yields_normalised_tenders() -> None:
    """Pattern 1: the 5 fixture rows pass through as NormalisedTenders."""
    html = load_text_fixture(FIXTURE)
    adapter = _adapter(static_text_handler(html, content_type="text/html"))

    tenders = await collect(adapter, datetime(2025, 1, 1, tzinfo=UTC))

    assert len(tenders) == 5
    for t in tenders:
        assert isinstance(t, NormalisedTender)
        assert t.source_code == "EU_SUPPLY"
        assert t.source_ref
        assert t.title
        assert t.status == "active"


async def test_fetch_since_ignores_cutoff_by_design() -> None:
    """Pattern 2, REVERSED on purpose (Phase-1 silent-source fix,
    2026-06-11): EU-Supply's listing shows currently-OPEN tenders whose
    publication date is often older than any poll window, and poll_source
    advances the watermark to `now` after every clean poll — so the old
    `published >= since` drop starved this source to zero rows forever.
    Listing-only adapters now RECONCILE the whole current listing every
    sweep; idempotency comes from the upsert change-hash, not the cutoff.
    A cutoff AFTER every fixture row's publication time must still yield
    all five rows."""
    html = load_text_fixture(FIXTURE)
    adapter = _adapter(static_text_handler(html, content_type="text/html"))

    cutoff = datetime(2026, 6, 9, 0, 0, tzinfo=UTC)  # later than all 5 rows
    tenders = await collect(adapter, cutoff)

    refs = {t.source_ref for t in tenders}
    assert len(tenders) == 5
    assert {"100111328", "100111094", "100111290"} <= refs
    # published_at is still PARSED and stored — only the drop is gone.
    assert all(t.published_at is not None for t in tenders)


async def test_normalisation_of_known_fields() -> None:
    """Pattern 3: the first row maps to its NormalisedTender exactly — the full
    tooltip values, the detail link (carrying PID), and the parsed datetimes."""
    html = load_text_fixture(FIXTURE)
    adapter = _adapter(static_text_handler(html, content_type="text/html"))

    tenders = await collect(adapter, datetime(2025, 1, 1, tzinfo=UTC))
    t = next(t for t in tenders if t.source_ref == "100111328")

    assert t.source_code == "EU_SUPPLY"
    assert t.procurement_ref == "TEAM01.10"
    assert t.title == "Key Stage 2 classroom modular build"
    assert t.buyer_name == "PHP Law LLP"
    assert t.buyer_country == "United Kingdom"
    assert t.notice_type == "Below Threshold Procurement"
    assert t.status == "active"
    # Detail link is absolutised onto the tenant host and carries the PID.
    assert t.source_url.startswith("https://yortender.eu-supply.com/app/rfq/")
    assert "PID=106219" in t.source_url
    # Full datetimes come from the tooltip title attribute.
    assert t.published_at == datetime(2026, 6, 8, 16, 46, 0, tzinfo=UTC)
    assert t.deadline_at == datetime(2026, 6, 29, 12, 0, 0, tzinfo=UTC)
    # EU-Supply exposes no region/CPV on the listing.
    assert t.buyer_region is None
    assert t.cpv_codes == []


_MINIMAL_ROW = """
<table class="ctm-table"><tbody>
<tr>
  <td><div title="999000111">999000111</div></td>
  <td><div></div></td>
  <td><a href="/app/rfq/rwlentrance_s.asp?PID=1">Bare minimum tender</a></td>
  <td><div title="01/06/2026 09:00:00">01/06/2026</div></td>
  <td></td>
  <td></td>
  <td><div></div></td>
  <td><div></div></td>
</tr>
</tbody></table>
"""


async def test_handles_missing_optional_fields() -> None:
    """Pattern 4: a row with empty reference/deadline/process/buyer/country still
    yields a tender; the optional fields are None (not empty strings)."""
    adapter = _adapter(static_text_handler(_MINIMAL_ROW, content_type="text/html"))

    tenders = await collect(adapter, datetime(2025, 1, 1, tzinfo=UTC))

    assert len(tenders) == 1
    t = tenders[0]
    assert t.source_ref == "999000111"
    assert t.title == "Bare minimum tender"
    assert "PID=1" in t.source_url
    assert t.procurement_ref is None
    assert t.buyer_name is None
    assert t.buyer_country is None
    assert t.deadline_at is None
    # Empty "Process" falls back to the generic notice type.
    assert t.notice_type == "tender"


_MALFORMED_PAGE = """
<table class="ctm-table"><tbody>
<tr><td colspan="8">No results found</td></tr>
<tr>
  <td><div title="100200300">100200300</div></td>
  <td><div title="REF-OK">REF-OK</div></td>
  <td><a href="/app/rfq/rwlentrance_s.asp?PID=42">Valid row survives</a></td>
  <td><div title="05/06/2026 10:00:00">05/06/2026</div></td>
  <td><div title="20/06/2026 12:00:00">20/06/2026</div></td>
  <td><div title="Open procedure">Open procedure</div></td>
  <td><div title="Acme Buyer">Acme Buyer</div></td>
  <td><div title="United Kingdom">United Kingdom</div></td>
</tr>
</tbody></table>
"""


async def test_handles_malformed_entry_gracefully() -> None:
    """Pattern 5: a short "no results" row (fewer than 8 cells) is skipped; the
    well-formed row in the same table still yields."""
    adapter = _adapter(
        static_text_handler(_MALFORMED_PAGE, content_type="text/html")
    )

    tenders = await collect(adapter, datetime(2025, 1, 1, tzinfo=UTC))

    assert len(tenders) == 1
    assert tenders[0].source_ref == "100200300"
    assert tenders[0].procurement_ref == "REF-OK"


async def test_sweeps_every_configured_tenant_host() -> None:
    """One adapter, parameterised by host: it fetches each configured tenant's
    public listing. Two hosts -> both are requested, and rows from both are
    yielded (cross-tenant dedup happens later, at upsert)."""
    captured: list[httpx.Request] = []
    html = load_text_fixture(FIXTURE)
    adapter = _adapter(
        static_text_handler(html, content_type="text/html", captured=captured),
        portals=[
            "https://yortender.eu-supply.com",
            "https://bluelight.eu-supply.com",
        ],
    )

    tenders = await collect(adapter, datetime(2025, 1, 1, tzinfo=UTC))

    hosts = {r.url.host for r in captured}
    assert hosts == {"yortender.eu-supply.com", "bluelight.eu-supply.com"}
    # Each host hits the public-tenders listing path.
    assert all(r.url.path == "/ctm/Supplier/PublicTenders" for r in captured)
    assert len(tenders) == 10  # 5 rows x 2 hosts


async def test_one_host_failure_does_not_stop_the_sweep() -> None:
    """A host returning an error is logged (had_errors) and skipped; other
    hosts still yield."""
    html = load_text_fixture(FIXTURE)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "down.eu-supply.com":
            return httpx.Response(503, text="unavailable")
        return httpx.Response(
            200, text=html, headers={"Content-Type": "text/html"}
        )

    adapter = _adapter(
        handler,
        portals=[
            "https://down.eu-supply.com",
            "https://yortender.eu-supply.com",
        ],
    )

    tenders = await collect(adapter, datetime(2025, 1, 1, tzinfo=UTC))

    assert adapter.had_errors is True
    assert len(tenders) == 5  # the healthy host still delivered


def test_default_portal_sweep_excludes_broken_blue_light_host() -> None:
    """bluelight.eu-supply.com was REMOVED from the DEFAULT sweep: the live
    2026-06-12T11:26Z sources-health readout proved its
    /ctm/Supplier/PublicTenders path 404s (that tenant has no public listing
    at the standard CTM path). The default keeps the working tenant(s) so
    EU-Supply keeps ingesting; a corrected Blue Light URL can be re-added by
    the operator. The host-parameterised sweep tests above prove the adapter
    still handles arbitrary hosts — this pins the default against a regression
    that re-introduces the 404-ing host."""
    from tender_agent.config import Settings

    default_portals = Settings.model_fields["eu_supply_portals"].default_factory()
    assert "https://bluelight.eu-supply.com" not in default_portals
    assert "https://yortender.eu-supply.com" in default_portals
