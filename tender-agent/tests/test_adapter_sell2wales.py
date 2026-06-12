"""Fixture-based tests for the Sell2Wales (S2W) adapter.

S2W shares its OCDS API codebase with PCS. The endpoint is month-granular
(`dateFrom=MM-YYYY`); the adapter iterates one request per month between
`since` and now. Tests pin month iteration to a single month so each test
makes exactly one upstream call.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from tender_agent.adapters import sell2wales as s2w_module
from tender_agent.adapters.sell2wales import Sell2WalesAdapter
from tender_agent.schemas import NormalisedTender

from .conftest import build_adapter, collect, load_json_fixture, static_json_handler

FIXTURE = "sell2wales_page.json"
CUTOFF = datetime(2026, 4, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _single_month(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(s2w_module, "_months_between", lambda _s, _e: [(4, 2026)])


async def test_consecutive_failures_abort_the_month_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Containment for a hard-down upstream (the 2026-06 expired-certificate
    outage): once CONSECUTIVE_FAILURE_LIMIT month fetches fail in a row, the
    sweep aborts instead of burning the full retry budget on every remaining
    month — a dead S2W must not slow the rest of the poll cycle."""
    monkeypatch.setattr(
        s2w_module,
        "_months_between",
        lambda _s, _e: [(m, 2026) for m in range(1, 6)],  # 5 months
    )
    adapter = build_adapter(
        Sell2WalesAdapter, static_json_handler({"releases": []})
    )
    calls = {"n": 0}

    async def failing_get_json(_url, params=None):
        calls["n"] += 1
        raise httpx.ConnectError("certificate verify failed: expired")

    adapter._get_json = failing_get_json  # bypass tenacity for test speed

    tenders = await collect(adapter, CUTOFF)

    assert tenders == []
    assert adapter.had_errors is True
    assert calls["n"] == Sell2WalesAdapter.CONSECUTIVE_FAILURE_LIMIT  # not 5
    assert len(adapter.error_messages) == Sell2WalesAdapter.CONSECUTIVE_FAILURE_LIMIT


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


async def test_fetch_since_sends_date_from_param() -> None:
    """S2W uses month granularity. The adapter sends `dateFrom=MM-YYYY` plus
    `noticeType`, `outputType=0` (OCDS), and `locale=2057` (English)."""
    payload = load_json_fixture(FIXTURE)
    captured: list[httpx.Request] = []
    adapter = build_adapter(
        Sell2WalesAdapter, static_json_handler(payload, captured=captured)
    )

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
