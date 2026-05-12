"""Fixture-based tests for the Sell2Wales (S2W) adapter.

S2W publishes OCDS release packages from `/Notices` with `pageSize=100` and
`updatedFrom=...`. Same normaliser as FTS/CF/PCS.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx

from tender_agent.adapters.sell2wales import Sell2WalesAdapter
from tender_agent.schemas import NormalisedTender

from .conftest import build_adapter, collect, load_json_fixture, static_json_handler

FIXTURE = "sell2wales_page.json"
CUTOFF = datetime(2025, 1, 1, tzinfo=UTC)


async def test_fetch_since_yields_normalised_tenders() -> None:
    payload = load_json_fixture(FIXTURE)
    adapter = build_adapter(Sell2WalesAdapter, static_json_handler(payload))

    tenders = await collect(adapter, CUTOFF)

    assert len(tenders) == 5
    for t in tenders:
        assert isinstance(t, NormalisedTender)
        assert t.source_code == "S2W"
        assert t.source_ref
        assert t.title


async def test_fetch_since_sends_updated_from_param() -> None:
    """S2W does NOT post-filter; it passes `updatedFrom` + `pageSize` to /Notices."""
    payload = load_json_fixture(FIXTURE)
    captured: list[httpx.Request] = []
    adapter = build_adapter(
        Sell2WalesAdapter, static_json_handler(payload, captured=captured)
    )

    cutoff = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
    await collect(adapter, cutoff)

    assert len(captured) >= 1
    request = captured[0]
    assert "/Notices" in str(request.url)
    assert request.url.params.get("updatedFrom") == "2026-03-01T12:00:00"
    assert request.url.params.get("pageSize") == "100"


async def test_normalisation_of_known_fields() -> None:
    payload = load_json_fixture(FIXTURE)
    adapter = build_adapter(Sell2WalesAdapter, static_json_handler(payload))

    tenders = await collect(adapter, CUTOFF)
    t = next(t for t in tenders if t.source_ref == "ocds-cy5x9q-s2w-2026-400001")

    assert t.title == "Estates security services — Welsh Government buildings"
    assert t.buyer_name == "Welsh Government"
    assert t.buyer_country == "United Kingdom"
    assert t.buyer_region == "Wales"
    assert t.notice_type == "tender"
    assert t.status == "active"
    assert t.value_amount == Decimal("2200000.0")
    assert t.value_currency == "GBP"
    assert t.cpv_codes == ["79710000"]
    assert t.deadline_at == datetime(2026, 6, 25, 17, 0, 0, tzinfo=UTC)
    assert t.source_url == (
        "https://www.sell2wales.gov.wales/search/show/search_view.aspx"
        "?ID=ocds-cy5x9q-s2w-2026-400001"
    )
    assert len(t.documents) == 1


async def test_handles_missing_optional_fields() -> None:
    payload = load_json_fixture(FIXTURE)
    adapter = build_adapter(Sell2WalesAdapter, static_json_handler(payload))

    tenders = await collect(adapter, CUTOFF)
    minimal = next(t for t in tenders if t.source_ref == "ocds-cy5x9q-s2w-2026-400005")

    assert minimal.description is None
    assert minimal.value_amount is None
    assert minimal.cpv_codes == []
    assert minimal.deadline_at is None
    assert minimal.documents == []
    assert minimal.buyer_name == "Snowdonia National Park Authority"


async def test_handles_malformed_entry_gracefully() -> None:
    payload = load_json_fixture(FIXTURE)
    adapter = build_adapter(Sell2WalesAdapter, static_json_handler(payload))

    tenders = await collect(adapter, CUTOFF)
    assert len(tenders) == 5
    titles = [t.title for t in tenders]
    assert not any("Malformed: no ocid" in title for title in titles)
