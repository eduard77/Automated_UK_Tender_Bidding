"""Fixture-based tests for the Public Contracts Scotland (PCS) adapter.

PCS publishes OCDS release packages from `/Notices`. The endpoint is month-
granular (`dateFrom=MM-YYYY`); the adapter iterates one request per month
between `since` and now. Tests pin month iteration to a single month so each
test makes exactly one upstream call.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from tender_agent.adapters import pcs as pcs_module
from tender_agent.adapters.pcs import PCSAdapter
from tender_agent.schemas import NormalisedTender

from .conftest import build_adapter, collect, load_json_fixture, static_json_handler

FIXTURE = "pcs_page.json"
CUTOFF = datetime(2026, 4, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _single_month(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the adapter to iterate one month so the static fixture handler is
    called once per test."""
    monkeypatch.setattr(pcs_module, "_months_between", lambda _s, _e: [(4, 2026)])


async def test_fetch_since_yields_normalised_tenders() -> None:
    payload = load_json_fixture(FIXTURE)
    adapter = build_adapter(PCSAdapter, static_json_handler(payload))

    tenders = await collect(adapter, CUTOFF)

    assert len(tenders) == 5
    for t in tenders:
        assert isinstance(t, NormalisedTender)
        assert t.source_code == "PCS"
        assert t.source_ref
        assert t.title


async def test_fetch_since_sends_date_from_param() -> None:
    """PCS uses month granularity. The adapter sends `dateFrom=MM-YYYY` plus
    `noticeType`, `outputType=0` (OCDS), and `locale=2057` (English) as
    documented by the Sell2Wales/PCS OCDS API."""
    payload = load_json_fixture(FIXTURE)
    captured: list[httpx.Request] = []
    adapter = build_adapter(PCSAdapter, static_json_handler(payload, captured=captured))

    await collect(adapter, CUTOFF)

    assert len(captured) == 1
    request = captured[0]
    assert "/Notices" in str(request.url)
    assert request.url.params.get("dateFrom") == "04-2026"
    assert request.url.params.get("outputType") == "0"
    assert request.url.params.get("locale") == "2057"
    assert request.url.params.get("noticeType") == "2"


async def test_normalisation_of_known_fields() -> None:
    payload = load_json_fixture(FIXTURE)
    adapter = build_adapter(PCSAdapter, static_json_handler(payload))

    tenders = await collect(adapter, CUTOFF)
    t = next(t for t in tenders if t.source_ref == "ocds-bya2v3-pcs-2026-300001")

    assert t.title == "Office cleaning — Scottish Government estate"
    assert t.buyer_name == "Scottish Government"
    assert t.buyer_country == "United Kingdom"
    assert t.buyer_region == "Scotland"
    assert t.notice_type == "tender"
    assert t.status == "active"
    assert t.value_amount == Decimal("1500000.0")
    assert t.value_currency == "GBP"
    assert t.cpv_codes == ["90910000"]
    assert t.deadline_at == datetime(2026, 6, 20, 17, 0, 0, tzinfo=UTC)
    assert t.source_url == (
        "https://www.publiccontractsscotland.gov.uk/search/show/"
        "search_view.aspx?ID=ocds-bya2v3-pcs-2026-300001"
    )
    assert len(t.documents) == 1


async def test_handles_missing_optional_fields() -> None:
    payload = load_json_fixture(FIXTURE)
    adapter = build_adapter(PCSAdapter, static_json_handler(payload))

    tenders = await collect(adapter, CUTOFF)
    minimal = next(t for t in tenders if t.source_ref == "ocds-bya2v3-pcs-2026-300005")

    assert minimal.description is None
    assert minimal.value_amount is None
    assert minimal.cpv_codes == []
    assert minimal.deadline_at is None
    assert minimal.documents == []
    assert minimal.buyer_name == "Orkney Islands Council"


async def test_handles_malformed_entry_gracefully() -> None:
    payload = load_json_fixture(FIXTURE)
    adapter = build_adapter(PCSAdapter, static_json_handler(payload))

    tenders = await collect(adapter, CUTOFF)
    assert len(tenders) == 5
    titles = [t.title for t in tenders]
    assert not any("Malformed: no ocid" in title for title in titles)
