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


async def test_malformed_feed_response_is_salvaged_and_reported() -> None:
    """2026-06-12 outage hardening: an unparseable page no longer kills the
    run silently OR invisibly — whatever releases CAN be salvaged are
    yielded, and the run is marked errored with a diagnosis naming the parse
    position and salvage count."""

    def handler(request: httpx.Request) -> httpx.Response:
        # 200 OK but the body is not parseable JSON.
        return httpx.Response(
            200,
            content=b"{ this is not valid json ",
            headers={"Content-Type": "application/json"},
        )

    adapter = build_adapter(FTSAdapter, handler)
    tenders = await collect(adapter, CUTOFF)

    assert tenders == []  # nothing salvageable from this body
    assert adapter.had_errors is True
    assert any("salvaged 0" in m for m in adapter.error_messages)


async def test_malformed_feed_on_later_page_keeps_earlier_records() -> None:
    """A bad batch MID-STREAM: page 1 returns good releases plus a `next`
    cursor, page 2 returns garbled JSON. The page-1 tenders survive, the run
    completes, and the failure is recorded with the salvage diagnosis."""
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
    assert adapter.had_errors is True  # the bad page is visible, not silent
    assert any("malformed JSON" in m for m in adapter.error_messages)


def _raw_release(ocid: str, date: str) -> str:
    return (
        f'{{"ocid": "{ocid}", "id": "{ocid}-1", "date": "{date}", '
        f'"tag": ["tender"], "tender": {{"title": "T {ocid}", '
        f'"status": "active"}}}}'
    )


async def test_bad_record_fails_alone_records_around_it_survive() -> None:
    """One malformed notice mid-array: the records BEFORE and AFTER it are
    salvaged (the scan resynchronises past the bad bytes); only the bad
    record is lost, and the diagnosis says so."""
    body = (
        '{"releases": ['
        + _raw_release("ocds-fts-good1", "2026-06-01T10:00:00Z")
        + ', {"ocid": "ocds-fts-bad", "date": }, '  # malformed record
        + _raw_release("ocds-fts-good2", "2026-06-02T10:00:00Z")
        + "]}"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body.encode(), headers={"Content-Type": "application/json"}
        )

    adapter = build_adapter(FTSAdapter, handler)
    tenders = await collect(adapter, CUTOFF)

    refs = {t.source_ref for t in tenders}
    assert "ocds-fts-good1" in refs
    assert "ocds-fts-good2" in refs  # the record AFTER the bad one survived
    assert "ocds-fts-bad" not in refs
    assert adapter.had_errors is True
    assert any("salvaged 2" in m for m in adapter.error_messages)


async def test_truncated_payload_salvages_prefix_and_advances_watermark() -> None:
    """The incident shape: a multi-MB body cut mid-stream. The complete
    releases before the cut are salvaged and — because earlier pages confirm
    the feed ascends — the watermark advances for what succeeded."""
    import json as jsonlib

    page1 = {
        "releases": [
            jsonlib.loads(_raw_release("ocds-fts-p1", "2026-06-01T10:00:00Z"))
        ],
        "links": {
            "next": "https://www.find-tender.service.gov.uk/api/1.0/"
            "ocdsReleasePackages?cursor=page2"
        },
    }
    # Page 2: two complete releases, then the body is CUT mid-record.
    page2_full = (
        '{"releases": ['
        + _raw_release("ocds-fts-p2a", "2026-06-03T10:00:00Z")
        + ", "
        + _raw_release("ocds-fts-p2b", "2026-06-04T10:00:00Z")
        + ", "
        + _raw_release("ocds-fts-p2c", "2026-06-05T10:00:00Z")
    )
    page2_truncated = page2_full[: page2_full.rfind("{")]  # cut mid-stream

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=page1)
        return httpx.Response(
            200,
            content=page2_truncated.encode(),
            headers={"Content-Type": "application/json"},
        )

    adapter = build_adapter(FTSAdapter, handler)
    tenders = await collect(adapter, CUTOFF)

    refs = {t.source_ref for t in tenders}
    assert {"ocds-fts-p1", "ocds-fts-p2a", "ocds-fts-p2b"} <= refs
    assert adapter.had_errors is True
    # Page 2's salvaged records confirmed page 1 ascends — the watermark
    # covers page 1, so the next run does not replay it.
    assert adapter.progress_watermark == datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
