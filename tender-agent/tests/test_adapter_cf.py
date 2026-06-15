"""Fixture-based tests for the Contracts Finder (CF) adapter.

CF publishes OCDS release packages from `/Search` with a `?limit=100` page size
and `updatedFrom=...` (note: camelCase, unlike FTS's kebab-case `updated-from`).
The normaliser is shared with FTS/PCS/S2W; tests here focus on CF-specific
routing and the camelCase param contract.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx

from tender_agent.adapters.contracts_finder import ContractsFinderAdapter
from tender_agent.schemas import NormalisedTender

from .conftest import build_adapter, collect, load_json_fixture, static_json_handler

FIXTURE = "cf_page.json"
CUTOFF = datetime(2025, 1, 1, tzinfo=UTC)


async def test_fetch_since_yields_normalised_tenders() -> None:
    payload = load_json_fixture(FIXTURE)
    adapter = build_adapter(ContractsFinderAdapter, static_json_handler(payload))

    tenders = await collect(adapter, CUTOFF)

    assert len(tenders) == 5
    for t in tenders:
        assert isinstance(t, NormalisedTender)
        assert t.source_code == "CF"
        assert t.source_ref
        assert t.title


async def test_fetch_since_sends_updated_from_param() -> None:
    """CF does NOT post-filter; it passes `updatedFrom` (camelCase) and `limit`
    to /Search. We assert both."""
    payload = load_json_fixture(FIXTURE)
    captured: list[httpx.Request] = []
    adapter = build_adapter(
        ContractsFinderAdapter, static_json_handler(payload, captured=captured)
    )

    cutoff = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
    await collect(adapter, cutoff)

    assert len(captured) >= 1
    request = captured[0]
    assert "/Search" in str(request.url)
    assert request.url.params.get("updatedFrom") == "2026-03-01T12:00:00"
    assert request.url.params.get("limit") == "100"


async def test_normalisation_of_known_fields() -> None:
    payload = load_json_fixture(FIXTURE)
    adapter = build_adapter(ContractsFinderAdapter, static_json_handler(payload))

    tenders = await collect(adapter, CUTOFF)
    t = next(t for t in tenders if t.source_ref == "ocds-b5fd17-cf-2026-200001")

    assert t.title == "Tier 1 + 2 IT service desk for the council estate"
    assert t.description.startswith("Two-year service desk contract")
    assert t.buyer_name == "Wiltshire Council"
    assert t.buyer_country == "United Kingdom"
    assert t.buyer_region == "South West"
    assert t.notice_type == "tender"
    assert t.status == "active"
    assert t.value_amount == Decimal("280000.0")
    assert t.value_currency == "GBP"
    assert t.cpv_codes == ["72253200", "72611000"]
    assert t.deadline_at == datetime(2026, 5, 30, 17, 0, 0, tzinfo=UTC)
    assert t.source_url == (
        "https://www.contractsfinder.service.gov.uk/Notice/ocds-b5fd17-cf-2026-200001"
    )
    assert len(t.documents) == 1
    assert t.documents[0].url.endswith("/spec.pdf")


async def test_handles_missing_optional_fields() -> None:
    payload = load_json_fixture(FIXTURE)
    adapter = build_adapter(ContractsFinderAdapter, static_json_handler(payload))

    tenders = await collect(adapter, CUTOFF)
    minimal = next(t for t in tenders if t.source_ref == "ocds-b5fd17-cf-2026-200005")

    assert minimal.description is None
    assert minimal.value_amount is None
    assert minimal.cpv_codes == []
    assert minimal.deadline_at is None
    assert minimal.documents == []
    assert minimal.buyer_name == "Borough of Hartlepool"


async def test_handles_malformed_entry_gracefully() -> None:
    payload = load_json_fixture(FIXTURE)
    adapter = build_adapter(ContractsFinderAdapter, static_json_handler(payload))

    tenders = await collect(adapter, CUTOFF)
    assert len(tenders) == 5
    titles = [t.title for t in tenders]
    assert not any("Malformed: no ocid" in title for title in titles)


# ---------------------------------------------------------------------------
# 429 handling + pacing + incremental watermark (2026-06-12 CF 429 spiral)
# ---------------------------------------------------------------------------


def _release(ocid: str, date: str) -> dict:
    return {
        "ocid": ocid,
        "id": f"{ocid}-1",
        "date": date,
        "tag": ["tender"],
        "tender": {"title": f"Tender {ocid}", "status": "active"},
    }


def _page(releases: list[dict], next_url: str | None) -> dict:
    payload: dict = {"releases": releases}
    if next_url:
        payload["links"] = {"next": next_url}
    return payload


def _no_sleep_adapter(handler) -> tuple[ContractsFinderAdapter, list[float]]:
    """Adapter with MockTransport + recorded (not slept) sleeps."""
    adapter = build_adapter(ContractsFinderAdapter, handler)
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    adapter._sleep = fake_sleep
    return adapter, sleeps


async def test_429_with_retry_after_is_honoured_then_succeeds() -> None:
    """One 429 carrying Retry-After: the adapter waits THAT long (not a
    blind hammer-retry) and the page then succeeds."""
    calls = {"n": 0}
    page = _page([_release("ocds-cf-1", "2026-06-10T10:00:00Z")], None)

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json=page)

    adapter, sleeps = _no_sleep_adapter(handler)
    tenders = await collect(adapter, CUTOFF)

    assert len(tenders) == 1
    assert adapter.had_errors is False
    assert 7.0 in sleeps  # the Retry-After value, honoured verbatim


async def test_persistent_429_aborts_gracefully_keeping_progress() -> None:
    """Pages 1+2 succeed, page 3 is 429 forever: the run aborts AFTER the
    bounded retry budget with a clear error — and the watermark covers the
    CONFIRMED pages, so the next run's window shrinks instead of replaying
    the whole backlog (the spiral)."""
    page1 = _page(
        [_release("ocds-cf-1", "2026-06-01T10:00:00Z")], "https://x.test/p2"
    )
    page2 = _page(
        [_release("ocds-cf-2", "2026-06-05T10:00:00Z")], "https://x.test/p3"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "/p2" in str(request.url):
            return httpx.Response(200, json=page2)
        if "/p3" in str(request.url):
            return httpx.Response(429)
        return httpx.Response(200, json=page1)

    adapter, sleeps = _no_sleep_adapter(handler)
    tenders = await collect(adapter, CUTOFF)

    assert len(tenders) == 2  # both good pages ingested before the abort
    assert adapter.had_errors is True
    assert any("429" in m for m in adapter.error_messages)
    # Pages 1 and 2 were both fully consumed and the feed ascends, so every
    # record dated <= page 2's max is behind us — the safe resume point is
    # page 2's max (page 3, which 429'd, never started). The retry's window
    # therefore shrinks to the page-3 region instead of replaying the backlog.
    assert adapter.progress_watermark == datetime(
        2026, 6, 5, 10, 0, tzinfo=UTC
    )
    # Backoff happened between 429 attempts (no Retry-After → exponential).
    assert any(s >= 10.0 for s in sleeps)


async def test_pages_are_paced_with_inter_page_delay() -> None:
    """Back-to-back page pulls tripped CF's rate limiter — pages after the
    first now wait INTER_PAGE_DELAY_S."""
    from tender_agent.adapters.contracts_finder import INTER_PAGE_DELAY_S

    page1 = _page(
        [_release("ocds-cf-1", "2026-06-01T10:00:00Z")], "https://x.test/p2"
    )
    page2 = _page([_release("ocds-cf-2", "2026-06-05T10:00:00Z")], None)

    def handler(request: httpx.Request) -> httpx.Response:
        if "/p2" in str(request.url):
            return httpx.Response(200, json=page2)
        return httpx.Response(200, json=page1)

    adapter, sleeps = _no_sleep_adapter(handler)
    await collect(adapter, CUTOFF)

    assert INTER_PAGE_DELAY_S in sleeps


async def test_descending_feed_never_advances_watermark() -> None:
    """Safety property of the confirm-then-advance tracker: if pages arrive
    NEWEST-first, a partial watermark would skip the older unfetched pages —
    so it must never advance."""
    page1 = _page(
        [_release("ocds-cf-9", "2026-06-09T10:00:00Z")], "https://x.test/p2"
    )
    page2 = _page(
        [_release("ocds-cf-1", "2026-06-01T10:00:00Z")], "https://x.test/p3"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "/p2" in str(request.url):
            return httpx.Response(200, json=page2)
        if "/p3" in str(request.url):
            return httpx.Response(429)
        return httpx.Response(200, json=page1)

    adapter, _sleeps = _no_sleep_adapter(handler)
    await collect(adapter, CUTOFF)

    assert adapter.progress_watermark is None  # frozen, never advanced
