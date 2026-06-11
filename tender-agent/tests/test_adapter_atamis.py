"""Atamis-on-Salesforce adapter — fixture tests (the five established patterns
plus pagination, multi-tenant sweep, and the real empty-tenant page).

Fixtures are REAL captures from the operator's browser (2026-06-10):
  atamis_listing_page.html  — NHS Health Family tenant, page 1 (10 rows,
                              "Page 1 of 491"), including rows with an EMPTY
                              Procurement Route and entity-escaped titles.
  atamis_listing_empty.html — UK Parliament tenant: same template, zero open
                              rows ("Page 1 of 0", empty tbody).
No network calls — httpx.MockTransport via the conftest helpers.
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx

from tender_agent.adapters.atamis import AtamisAdapter
from tests.conftest import collect, load_text_fixture, static_text_handler

FIXTURE = "atamis_listing_page.html"
EMPTY_FIXTURE = "atamis_listing_empty.html"
HEALTH = "https://atamis-1928.my.salesforce-sites.com"
PARLIAMENT = "https://atamis-ukparliament.my.salesforce-sites.com"

_OLD = datetime(2020, 1, 1, tzinfo=UTC)


def _adapter(handler, portals=None, max_pages=1) -> AtamisAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return AtamisAdapter(
        client=client, portals=portals or [HEALTH], max_pages=max_pages
    )


# --- pattern 1: full-fixture round-trip --------------------------------------


async def test_fetch_since_yields_normalised_tenders() -> None:
    html = load_text_fixture(FIXTURE)
    adapter = _adapter(static_text_handler(html, content_type="text/html"))

    tenders = await collect(adapter, _OLD)

    assert len(tenders) == 10
    assert all(t.source_code == "ATAMIS" for t in tenders)
    # Every row carries the Salesforce uid (source_ref) AND the Atamis
    # reference (procurement_ref — the cross-source dedup key).
    assert all(t.source_ref and len(t.source_ref) == 18 for t in tenders)
    assert all(t.procurement_ref and t.procurement_ref.startswith("C") for t in tenders)


# --- pattern 2: cutoff (post-filtering adapter) -------------------------------


async def test_fetch_since_ignores_cutoff_by_design() -> None:
    """Pattern 2, REVERSED on purpose (Phase-1 silent-source fix,
    2026-06-11): poll_source advances the source watermark to `now` after
    every clean poll, so the old Opens-based drop left only a ~30-minute
    window after the first cycle — starving the source. Each sweep now
    RECONCILES the newest pages (bounded by ATAMIS_MAX_PAGES); idempotency
    comes from the upsert change-hash. The two re-published 2022/2024 rows
    on the real capture must now be KEPT alongside the other eight."""
    html = load_text_fixture(FIXTURE)
    adapter = _adapter(static_text_handler(html, content_type="text/html"))

    tenders = await collect(adapter, datetime(2026, 1, 1, tzinfo=UTC))

    refs = {t.procurement_ref for t in tenders}
    assert len(tenders) == 10
    assert "C452691" in refs  # Opens 02/05/2022 — re-published, still open
    assert "C445365" in refs  # Opens 25/06/2024 — re-published, still open
    assert "C449892" in refs  # Opens 03/06/2026


# --- pattern 3: exact field mapping on one known row ---------------------------


async def test_normalisation_of_known_fields() -> None:
    html = load_text_fixture(FIXTURE)
    adapter = _adapter(static_text_handler(html, content_type="text/html"))

    tenders = await collect(adapter, _OLD)
    t = next(x for x in tenders if x.procurement_ref == "C449892")

    assert t.source_ref == "a07Pz00001cjxCKIAY"  # the uid in the detail link
    assert t.title == "UCLH-7683 - Second Line Leader Programme"
    assert t.buyer_name == (
        "University College London Hospitals NHS Foundation Trust"
    )
    assert t.notice_type == "PA23 Below Threshold - Limited Competition"
    assert t.published_at == datetime(2026, 6, 3, tzinfo=UTC)
    assert t.deadline_at == datetime(2026, 6, 24, 12, 0, tzinfo=UTC)
    # Detail link: the host + the captured ProSpend__CS_ContractPage href.
    assert t.source_url.startswith(f"{HEALTH}/ProSpend__CS_ContractPage")
    assert "uid=a07Pz00001cjxCKIAY" in t.source_url
    assert t.raw["discovered_via"] == "atamis_visualforce_listing"
    # Entity-escaped titles decode ("N & WY", not "N &amp; WY").
    t2 = next(x for x in tenders if x.procurement_ref == "C452044")
    assert "N & WY" in t2.title


# --- pattern 4: missing optionals stay None/default ----------------------------


async def test_handles_missing_optional_fields() -> None:
    """Two REAL captured rows have an empty Procurement Route — the adapter
    falls back to the generic notice_type and everything else still maps."""
    html = load_text_fixture(FIXTURE)
    adapter = _adapter(static_text_handler(html, content_type="text/html"))

    tenders = await collect(adapter, _OLD)
    no_route = [t for t in tenders if t.procurement_ref in {"C452044", "C452231"}]
    assert len(no_route) == 2
    assert all(t.notice_type == "tender" for t in no_route)
    assert all(t.deadline_at is not None for t in no_route)


# --- pattern 5: malformed row skipped, others survive ---------------------------


async def test_handles_malformed_entry_gracefully() -> None:
    html = load_text_fixture(FIXTURE)
    # Break ONE row's detail link so it has no uid; inject one structurally
    # bogus row too. The other nine must still parse.
    html = html.replace(
        "uid=a07Pz00001cjxCKIAY", "uid=", 2  # both anchors in the first row
    )
    html = html.replace(
        "<tbody>", "<tbody><tr><td>not a data row</td></tr>", 1
    )
    adapter = _adapter(static_text_handler(html, content_type="text/html"))

    tenders = await collect(adapter, _OLD)

    refs = {t.procurement_ref for t in tenders}
    assert len(tenders) == 9
    assert "C449892" not in refs  # the de-uid'd row was skipped
    assert "C452691" in refs


# --- empty tenant (real UK Parliament capture) ---------------------------------


async def test_empty_tenant_yields_nothing_without_error() -> None:
    html = load_text_fixture(EMPTY_FIXTURE)  # "Page 1 of 0", empty tbody
    adapter = _adapter(
        static_text_handler(html, content_type="text/html"),
        portals=[PARLIAMENT],
    )

    tenders = await collect(adapter, _OLD)

    assert tenders == []
    assert adapter.had_errors is False


# --- pagination -----------------------------------------------------------------


async def test_walks_pages_up_to_cap_with_page_param() -> None:
    """'Page 1 of 491' on the capture; with a cap of 2 the adapter requests
    page=1 then page=2 and stops. (The mock serves the same fixture for both,
    so the identical-uid guard also proves rows aren't double-yielded.)"""
    captured: list[httpx.Request] = []
    html = load_text_fixture(FIXTURE)
    adapter = _adapter(
        static_text_handler(html, content_type="text/html", captured=captured),
        max_pages=2,
    )

    tenders = await collect(adapter, _OLD)

    pages = [r.url.params.get("page") for r in captured]
    assert pages == ["1", "2"]
    assert all(
        r.url.params.get("searchtype") == "Projects"
        and r.url.params.get("sortStr") == "Recently Published"
        for r in captured
    )
    # Same 10 uids served twice -> yielded once.
    assert len(tenders) == 10


async def test_stops_when_listing_reports_fewer_pages_than_cap() -> None:
    """The empty tenant reports 'Page 1 of 0' — one request, no spinning to
    the cap."""
    captured: list[httpx.Request] = []
    html = load_text_fixture(EMPTY_FIXTURE)
    adapter = _adapter(
        static_text_handler(html, content_type="text/html", captured=captured),
        portals=[PARLIAMENT],
        max_pages=5,
    )

    await collect(adapter, _OLD)

    assert len(captured) == 1


# --- multi-tenant sweep -----------------------------------------------------------


async def test_sweeps_every_configured_tenant_host() -> None:
    captured: list[httpx.Request] = []
    health_html = load_text_fixture(FIXTURE)
    empty_html = load_text_fixture(EMPTY_FIXTURE)

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = health_html if "1928" in request.url.host else empty_html
        return httpx.Response(
            200, content=body.encode(), headers={"Content-Type": "text/html"}
        )

    adapter = _adapter(handler, portals=[HEALTH, PARLIAMENT])
    tenders = await collect(adapter, _OLD)

    hosts = {r.url.host for r in captured}
    assert hosts == {
        "atamis-1928.my.salesforce-sites.com",
        "atamis-ukparliament.my.salesforce-sites.com",
    }
    assert len(tenders) == 10  # health rows only; parliament is empty


async def test_one_host_failure_does_not_stop_the_sweep() -> None:
    health_html = load_text_fixture(FIXTURE)

    def handler(request: httpx.Request) -> httpx.Response:
        if "ukparliament" in request.url.host:
            return httpx.Response(500, text="boom")
        return httpx.Response(
            200,
            content=health_html.encode(),
            headers={"Content-Type": "text/html"},
        )

    adapter = _adapter(handler, portals=[PARLIAMENT, HEALTH])
    tenders = await collect(adapter, _OLD)

    assert len(tenders) == 10
    assert adapter.had_errors is True


# --- registration ------------------------------------------------------------------


def test_adapter_registered_under_atamis_code() -> None:
    from tender_agent.adapters import ADAPTERS

    assert ADAPTERS["ATAMIS"] is AtamisAdapter
    assert AtamisAdapter.code == "ATAMIS"
