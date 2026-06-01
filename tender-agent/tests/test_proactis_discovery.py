"""Tests for the Proactis discovery service (Phase 4 chunk 9).

All tests are 100% offline. The bridge is mocked end-to-end via an
``AsyncMock`` BridgeClient. The DB path goes through ``_upsert_tender`` /
``find_duplicate`` / ``resolve_for_tender`` exactly as CF/FTS do, so the
"uses the same path" assertion is structural (we patch those functions on
the discovery module and verify they're invoked).
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tender_agent.services.discovery import proactis_discovery as pd
from tender_agent.services.discovery.proactis_filter_config import (
    ProactisFilterConfig,
)
from tender_agent.services.portals.adapters.proactis import (
    OPPORTUNITY_ID_RE,
    OPPORTUNITY_VALUE_RE,
    PROACTIS_URLS,
)

# ---------------------------------------------------------------------------
# Bridge fakes
# ---------------------------------------------------------------------------


def _fake_bridge_authenticated() -> AsyncMock:
    """A bridge whose `session_status` always reports a post-login URL.

    Tests that don't care about auth path use this as their default.
    """
    bridge = AsyncMock()
    bridge.open_session = AsyncMock(return_value={})
    bridge.navigate = AsyncMock(return_value={})
    bridge.session_status = AsyncMock(
        return_value={"current_url": PROACTIS_URLS["post_login_home"]}
    )
    bridge.wait_for_login = AsyncMock(return_value={})
    bridge.fill = AsyncMock(return_value={})
    bridge.click = AsyncMock(return_value={})
    bridge.select_option = AsyncMock(return_value={})
    bridge.element_exists = AsyncMock(return_value=False)
    bridge.close_session = AsyncMock(return_value={})
    bridge.rendered_html = AsyncMock(
        return_value=MagicMock(
            html="", wait_satisfied=True, current_url=PROACTIS_URLS["opportunities"]
        )
    )
    bridge.page_text = AsyncMock(return_value="")
    return bridge


# ---------------------------------------------------------------------------
# _apply_filters
# ---------------------------------------------------------------------------


async def test_apply_filters_sets_keywords_includeclosed_regions_and_clicks_update() -> None:
    bridge = _fake_bridge_authenticated()
    cfg = ProactisFilterConfig(
        keywords="cleaning",
        regions=["North West", "Merseyside"],
        categories=["Cleaning services"],
        include_closed=False,
    )
    await pd._apply_filters(bridge, cfg)

    # Keywords typed in.
    keyword_calls = [c for c in bridge.fill.call_args_list if "cleaning" in c.args]
    assert keyword_calls, "keywords were not typed into the keyword input"

    # include_closed=False → select 'No'.
    select_labels = [c.kwargs.get("label") for c in bridge.select_option.call_args_list]
    assert "No" in select_labels

    # Update button clicked at least once (regions / categories also click).
    clicked = [c.args[1] for c in bridge.click.call_args_list]
    assert any("Update" in sel for sel in clicked)

    # Two regions added → two "Add new region" clicks + two region fills.
    region_clicks = [s for s in clicked if "Add new region" in s]
    assert len(region_clicks) == 2
    region_fills = [
        c for c in bridge.fill.call_args_list
        if any(name in c.args for name in ("North West", "Merseyside"))
    ]
    assert len(region_fills) == 2


async def test_apply_filters_skips_unconstrained_keyword() -> None:
    """An empty keyword string must not fire a fill on the keyword input —
    otherwise we'd clear a leftover server-side keyword."""
    bridge = _fake_bridge_authenticated()
    cfg = ProactisFilterConfig(keywords="   ")
    await pd._apply_filters(bridge, cfg)
    fills = [c.args for c in bridge.fill.call_args_list]
    assert not any("" in args or "   " in args for args in fills)


# ---------------------------------------------------------------------------
# Listing parse + walk
# ---------------------------------------------------------------------------


_LISTING_PAGE_1 = """
<table class="opportunities"><tbody>
<tr><td>
  <a href="/Supplier/Advert/View?advertId=aaaa1111-bbbb-cccc-dddd-eeeeffff0001">
    Cleaning services for South West
  </a>
</td><td>Bristol City Council</td><td>£480,000.00</td></tr>
<tr><td>
  <a href="/Supplier/Advert/View?advertId=aaaa1111-bbbb-cccc-dddd-eeeeffff0002">
    IT service desk
  </a>
</td><td>Wiltshire Council</td><td>£280,000.00</td></tr>
</tbody></table>
"""

_LISTING_PAGE_2 = """
<table class="opportunities"><tbody>
<tr><td>
  <a href="https://procontract.due-north.com/Supplier/Advert/View?advertId=aaaa1111-bbbb-cccc-dddd-eeeeffff0003">
    Office cleaning — Scottish Government estate
  </a>
</td><td>Scottish Government</td><td>£1,500,000.00</td></tr>
</tbody></table>
"""


def test_parse_listing_rows_extracts_advert_id_and_title() -> None:
    rows = pd._parse_listing_rows(_LISTING_PAGE_1)
    assert len(rows) == 2
    assert rows[0].advert_id == "aaaa1111-bbbb-cccc-dddd-eeeeffff0001"
    assert rows[0].title == "Cleaning services for South West"
    assert rows[0].detail_url and "advertId=aaaa1111" in rows[0].detail_url


async def test_walk_listing_paginates_until_next_link_absent() -> None:
    bridge = _fake_bridge_authenticated()
    pages = [_LISTING_PAGE_1, _LISTING_PAGE_2]
    page_iter = iter(pages)

    async def rendered_side_effect(*args, **kwargs):
        try:
            html = next(page_iter)
        except StopIteration:
            html = ""
        return MagicMock(html=html, wait_satisfied=True, current_url="")

    bridge.rendered_html = AsyncMock(side_effect=rendered_side_effect)
    # Next link exists for the first page only.
    bridge.element_exists = AsyncMock(side_effect=[True, False])

    rows = []
    async for r in pd._walk_listing(bridge, ProactisFilterConfig(max_pages=10)):
        rows.append(r)

    assert pd._last_walked_pages == 2
    assert [r.advert_id[-4:] for r in rows] == ["0001", "0002", "0003"]


async def test_walk_listing_respects_max_pages_cap() -> None:
    bridge = _fake_bridge_authenticated()
    # Each page must yield NEW rows, otherwise the dedupe-within-run guard
    # would short-circuit before we hit the cap. So generate distinct advertIds
    # per page; we want to prove the cap stops us even when the source has more.
    def _page(n: int) -> str:
        return (
            '<table class="opportunities"><tbody>'
            f'<tr><td><a href="/Supplier/Advert/View?advertId=cccc1111-bbbb-cccc-dddd-eeeeffff000{n}">'
            f'Row page {n}</a></td><td>Buyer {n}</td><td>£100,000.00</td></tr>'
            '</tbody></table>'
        )

    page_counter = {"i": 0}

    async def rendered_side_effect(*args, **kwargs):
        page_counter["i"] += 1
        return MagicMock(
            html=_page(page_counter["i"]), wait_satisfied=True, current_url=""
        )

    bridge.rendered_html = AsyncMock(side_effect=rendered_side_effect)
    bridge.element_exists = AsyncMock(return_value=True)  # always-Next
    bridge.click = AsyncMock(return_value=None)

    rows = []
    async for r in pd._walk_listing(bridge, ProactisFilterConfig(max_pages=3)):
        rows.append(r)

    assert pd._last_walked_pages == 3
    assert len(rows) == 3


# ---------------------------------------------------------------------------
# Detail read — DN + value + region + dates
# ---------------------------------------------------------------------------


_DETAIL_TEXT = """
Opportunity Id: DN815596

Title: Cleaning services for primary schools, South West region
Buyer: Bristol City Council
Region(s) of supply: Merseyside
Estimated value: £192,000.00

Categories: 76111501, Cleaning services

Description
Daily and weekly cleaning across 42 primary schools. Three-year contract
with options for two further years.

Expression start: 01/06/2026
Expression end: 15/06/2026

Contract start: 01/09/2026
Contract end: 31/08/2029

Keywords: cleaning, schools
"""


async def test_read_detail_extracts_dn_value_region_dates() -> None:
    bridge = _fake_bridge_authenticated()
    bridge.page_text = AsyncMock(return_value=_DETAIL_TEXT)

    row = pd.DiscoveredOpportunity(
        advert_id="aaaa1111-bbbb-cccc-dddd-eeeeffff0001",
        dn_reference=None,
        title="Cleaning services for primary schools, South West region",
    )
    detail = await pd._read_detail(bridge, row)

    assert detail.dn_reference == "DN815596"
    assert detail.value_amount == Decimal("192000.00")
    assert detail.value_currency == "GBP"
    assert detail.buyer_region == "Merseyside"
    assert detail.buyer_name == "Bristol City Council"
    assert detail.expression_end == datetime(2026, 6, 15, tzinfo=UTC)
    assert detail.contract_start and detail.contract_start.isoformat() == "2026-09-01"
    assert detail.contract_end and detail.contract_end.isoformat() == "2029-08-31"
    assert "cleaning" in detail.keywords
    assert detail.is_complete_for_dedup() is True


def test_value_regex_handles_no_pennies() -> None:
    """Proactis sometimes writes the value as £192,000 with no decimals."""
    m = OPPORTUNITY_VALUE_RE.search("Estimated value: £192,000")
    assert m and m.group(1).replace(",", "") == "192000"


def test_dn_regex_does_not_match_random_dn_substrings() -> None:
    """Guard against false-positive matches that look like DN but aren't a
    full reference (e.g. 'DN1' is too short)."""
    assert OPPORTUNITY_ID_RE.search("DN123") is None
    assert OPPORTUNITY_ID_RE.search("DN1234").group(0) == "DN1234"


# ---------------------------------------------------------------------------
# Upsert + dedup — uses the SAME path as CF
# ---------------------------------------------------------------------------


def _make_opportunity(
    *, advert_id: str = "guid-1", dn: str | None = "DN815596"
) -> pd.DiscoveredOpportunity:
    return pd.DiscoveredOpportunity(
        advert_id=advert_id,
        dn_reference=dn,
        title="Cleaning services",
        buyer_name="Bristol City Council",
        buyer_region="Merseyside",
        value_amount=Decimal("192000.00"),
        expression_end=datetime(2026, 6, 15, tzinfo=UTC),
        keywords=["cleaning"],
    )


def test_upsert_routes_through_existing_upsert_tender_with_proactis_source_code() -> None:
    """The discovery service MUST funnel through `_upsert_tender`. We patch
    the import that proactis_discovery uses (a re-export of the same name)
    and confirm it sees the right NormalisedTender shape, with source_code
    'PROACTIS' and procurement_ref = DN reference."""
    fake_tender = MagicMock()
    fake_tender.duplicate_of_id = None
    with patch.object(
        pd, "_upsert_tender", return_value=(fake_tender, "new")
    ) as upsert:
        opp = _make_opportunity()
        action, deduped = pd._upsert_from_discovered(MagicMock(), opp)

    assert action == "new"
    assert deduped is False
    args, _kwargs = upsert.call_args
    normalised = args[1]
    assert normalised.source_code == "PROACTIS"
    assert normalised.source_ref == "guid-1"
    assert normalised.procurement_ref == "DN815596"
    assert normalised.buyer_region == "Merseyside"
    assert normalised.value_amount == Decimal("192000.00")
    assert normalised.deadline_at == datetime(2026, 6, 15, tzinfo=UTC)


def test_upsert_marks_deduped_when_duplicate_of_id_set() -> None:
    """When `_upsert_tender`'s own dedup links the new row to an existing CF
    tender (sets `duplicate_of_id`), the discovery summary records it."""
    fake_tender = MagicMock()
    fake_tender.duplicate_of_id = 42  # set by find_duplicate inside _upsert_tender
    with patch.object(
        pd, "_upsert_tender", return_value=(fake_tender, "new")
    ):
        action, deduped = pd._upsert_from_discovered(
            MagicMock(), _make_opportunity()
        )
    assert action == "new"
    assert deduped is True


def test_upsert_idempotent_unchanged_returns_unchanged() -> None:
    fake_tender = MagicMock()
    fake_tender.duplicate_of_id = None
    with patch.object(
        pd, "_upsert_tender", return_value=(fake_tender, "unchanged")
    ):
        action, deduped = pd._upsert_from_discovered(
            MagicMock(), _make_opportunity()
        )
    assert action == "unchanged"
    assert deduped is False


# ---------------------------------------------------------------------------
# Top-level `run()` — needs_login path
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_db_factory() -> Iterator[MagicMock]:
    """A db_factory whose context-manager session is a MagicMock. Discovery
    only needs it to (a) create a Source/PollRun (b) commit. We stub both."""
    session = MagicMock()
    # _ensure_proactis_source path: existing Source -> return it.
    existing_source = MagicMock(id=1)
    session.execute.return_value.scalar_one_or_none.return_value = existing_source
    # PollRun get-by-id path in _finalise_poll_run.
    fake_poll_run = MagicMock(id=99)
    session.get.return_value = fake_poll_run
    session.add = MagicMock()
    session.commit = MagicMock()
    session.refresh = MagicMock(side_effect=lambda obj: setattr(obj, "id", 99))

    factory = MagicMock()
    factory.return_value.__enter__.return_value = session
    factory.return_value.__exit__.return_value = False
    yield factory


async def test_run_needs_login_when_bridge_stays_on_login_url(stub_db_factory) -> None:
    bridge = _fake_bridge_authenticated()
    # Force the auth probe to keep returning a login URL even after the
    # wait_for_login round-trip.
    bridge.session_status = AsyncMock(
        return_value={"current_url": PROACTIS_URLS["login"]}
    )
    result = await pd.run(
        config=ProactisFilterConfig(),
        bridge=bridge,
        db_factory=stub_db_factory,
    )
    assert result.status == "needs_login"
    # Bridge should still get released.
    bridge.close_session.assert_awaited()


async def test_run_records_pollrun_finalisation_on_success(stub_db_factory) -> None:
    """`_finalise_poll_run` must always run — happy path and error path —
    so the audit row gets the final status, counts, and finished_at."""
    bridge = _fake_bridge_authenticated()
    # Make the listing walk yield nothing — fast path.
    bridge.element_exists = AsyncMock(return_value=False)

    result = await pd.run(
        config=ProactisFilterConfig(max_pages=1),
        bridge=bridge,
        db_factory=stub_db_factory,
    )
    assert result.status == "ok"
    assert result.poll_run_id == 99
