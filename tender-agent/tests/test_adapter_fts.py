"""Fixture-based tests for the Find a Tender (FTS) adapter.

FTS publishes OCDS release packages. The normaliser is shared across FTS, CF,
PCS, and S2W; these tests focus on the FTS adapter's own behaviour: routing,
upstream query params, and error swallowing around the shared normaliser.

All HTTP is mocked via httpx.MockTransport; tests run fully offline.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx

from tender_agent.adapters.fts import FTSAdapter
from tender_agent.schemas import NormalisedTender

from .conftest import build_adapter, collect, load_json_fixture, static_json_handler

FIXTURE = "fts_page.json"
CUTOFF = datetime(2025, 1, 1, tzinfo=UTC)


async def test_fetch_since_yields_normalised_tenders() -> None:
    """Pattern 1: full fixture round-trips into 5 NormalisedTender objects.

    The 6th release in the fixture is malformed (no ocid + no id) and the
    normaliser raises; the adapter logs and continues, so we expect 5 yielded.
    """
    payload = load_json_fixture(FIXTURE)
    adapter = build_adapter(FTSAdapter, static_json_handler(payload))

    tenders = await collect(adapter, CUTOFF)

    assert len(tenders) == 5
    for t in tenders:
        assert isinstance(t, NormalisedTender)
        assert t.source_code == "FTS"
        assert t.source_ref  # ocid is required, never empty
        assert t.title  # normaliser falls back to "(untitled)" if missing


async def test_fetch_since_sends_updated_from_param() -> None:
    """Pattern 2 (OCDS variant): the FTS adapter does NOT post-filter on `since`
    — it passes `updatedFrom` upstream and trusts the response. The Z suffix is
    required by FTS; without it the API returns 400.
    """
    payload = load_json_fixture(FIXTURE)
    captured: list[httpx.Request] = []
    adapter = build_adapter(FTSAdapter, static_json_handler(payload, captured=captured))

    cutoff = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
    await collect(adapter, cutoff)

    assert len(captured) >= 1
    request = captured[0]
    assert "/ocdsReleasePackages" in str(request.url)
    assert request.url.params.get("updatedFrom") == "2026-03-01T12:00:00Z"


async def test_normalisation_of_known_fields() -> None:
    """Pattern 3: the first release maps to its NormalisedTender exactly."""
    payload = load_json_fixture(FIXTURE)
    adapter = build_adapter(FTSAdapter, static_json_handler(payload))

    tenders = await collect(adapter, CUTOFF)
    t = next(t for t in tenders if t.source_ref == "ocds-h6vhqg-fts-2026-100001")

    assert t.title == "Cleaning services for primary schools, South West region"
    assert t.description.startswith("Daily and weekly cleaning")
    assert t.buyer_name == "Bristol City Council"
    assert t.buyer_id == "GB-LAS-BCC"
    assert t.buyer_country == "United Kingdom"
    assert t.buyer_region == "South West"
    assert t.notice_type == "tender"
    assert t.status == "active"
    assert t.value_amount == Decimal("480000.0")
    assert t.value_currency == "GBP"
    # Both classification.id (90910000) and items[].additionalClassifications
    # (90919300) should be collected, deduped and sorted.
    assert t.cpv_codes == ["90910000", "90919300"]
    assert t.deadline_at is not None
    assert t.deadline_at.tzinfo is not None
    assert t.deadline_at == datetime(2026, 6, 15, 17, 0, 0, tzinfo=UTC)
    assert t.published_at == datetime(2026, 4, 28, 11, 30, 0, tzinfo=UTC)
    assert t.source_url == (
        "https://www.find-tender.service.gov.uk/Notice/ocds-h6vhqg-fts-2026-100001"
    )
    assert len(t.documents) == 2
    assert t.documents[0].url.endswith("/itt.pdf")
    assert t.documents[0].format == "application/pdf"


async def test_handles_missing_optional_fields() -> None:
    """Pattern 4: a release with no description, no value, no CPV codes, no
    documents should still yield a NormalisedTender with those fields = None
    / empty list (not the literal string "None")."""
    payload = load_json_fixture(FIXTURE)
    adapter = build_adapter(FTSAdapter, static_json_handler(payload))

    tenders = await collect(adapter, CUTOFF)
    minimal = next(t for t in tenders if t.source_ref == "ocds-h6vhqg-fts-2026-100005")

    assert minimal.title == "Grounds maintenance — under-threshold notice"
    assert minimal.description is None
    assert minimal.value_amount is None
    assert minimal.value_currency is None
    assert minimal.cpv_codes == []
    assert minimal.deadline_at is None
    assert minimal.documents == []
    assert minimal.buyer_name == "Mid Sussex District Council"
    # Buyer fields not resolvable from parties: stays None, not the string "None".
    assert minimal.buyer_country is None
    assert minimal.buyer_region is None


async def test_handles_malformed_entry_gracefully() -> None:
    """Pattern 5: an entry with no ocid + no id is skipped (normaliser raises
    ValueError; adapter catches and logs). The remaining entries on the page
    still yield."""
    payload = load_json_fixture(FIXTURE)
    adapter = build_adapter(FTSAdapter, static_json_handler(payload))

    tenders = await collect(adapter, CUTOFF)
    # 6 releases in fixture, 1 malformed → 5 yielded.
    assert len(tenders) == 5
    # The malformed release has a distinctive title; we never see it.
    titles = [t.title for t in tenders]
    assert not any("Malformed: no ocid" in title for title in titles)


async def test_malformed_feed_response_does_not_fail_run() -> None:
    """Phase-1 resilience (2026-06-12): a malformed / truncated feed response
    (HTTP 200 but the body isn't valid JSON) is caught and skipped — the run
    COMPLETES instead of erroring the whole source. `had_errors` stays False so
    one transient bad batch can't flip FTS to fetch_failing."""

    def handler(request: httpx.Request) -> httpx.Response:
        # 200 OK but the body is not parseable JSON -> response.json() raises.
        return httpx.Response(
            200,
            content=b"{ this is not valid json ",
            headers={"Content-Type": "application/json"},
        )

    adapter = build_adapter(FTSAdapter, handler)
    tenders = await collect(adapter, CUTOFF)

    assert tenders == []  # nothing yielded from the unparseable page
    assert adapter.had_errors is False  # but the run is NOT marked failed


async def test_malformed_feed_on_later_page_keeps_earlier_records() -> None:
    """A bad batch MID-STREAM: page 1 returns good releases plus a `next`
    cursor, page 2 returns malformed JSON. The page-1 tenders survive and the
    run completes (had_errors False) — the bad batch is skipped, not fatal."""
    page1 = load_json_fixture(FIXTURE)
    page1 = {
        **page1,
        "links": {
            "next": "https://www.find-tender.service.gov.uk/api/1.0/"
            "ocdsReleasePackages?cursor=page2"
        },
    }
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=page1)
        # Second page is a truncated/garbled response.
        return httpx.Response(
            200,
            content=b'{"releases": [ {',
            headers={"Content-Type": "application/json"},
        )

    adapter = build_adapter(FTSAdapter, handler)
    tenders = await collect(adapter, CUTOFF)

    assert calls["n"] == 2  # it did follow the cursor to page 2
    assert len(tenders) == 5  # page 1's good releases stand
    assert adapter.had_errors is False  # page 2's bad batch didn't fail the run
