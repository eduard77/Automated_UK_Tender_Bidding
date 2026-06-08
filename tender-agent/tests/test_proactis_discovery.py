"""Tests for the public-HTTP Proactis discovery service.

All tests are 100% offline: discovery fetches the public "Find Opportunities"
listing over plain HTTP, so we inject a fake ``fetch`` that returns saved HTML
strings — no network, no bridge, no login. The DB path goes through
``_upsert_tender`` / ``find_duplicate`` / ``resolve_for_tender`` exactly as
CF/FTS do, so the "uses the same path" assertion is structural (we patch
``_upsert_tender`` on the discovery module and verify it's invoked with the
right ``NormalisedTender`` shape).
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from tender_agent.services.discovery import proactis_discovery as pd
from tender_agent.services.discovery.proactis_filter_config import (
    ProactisFilterConfig,
)

# ---------------------------------------------------------------------------
# Sample public listing HTML
# ---------------------------------------------------------------------------

# A realistic row: title anchor links to /Advert?advertId={GUID} and carries the
# DN reference in its `title` attribute; cells give buyer, expression dates and
# the estimated value ("£1,400,000.00"). The second row uses an absolute href,
# an "N/A" value, and a single (closing) date.
_LISTING_PAGE_1 = """
<table class="opportunities">
  <thead><tr><th>Title</th><th>Organisation</th><th>Start</th><th>Closing</th><th>Value</th></tr></thead>
  <tbody>
    <tr>
      <td><a href="/Advert?advertId=aaaa1111-bbbb-cccc-dddd-eeeeffff0001&amp;p=1"
             title="DN817867">Tier 1 &amp; 2 IT service desk</a></td>
      <td>Wiltshire Council</td>
      <td>16/05/2026 09:00</td>
      <td>30/05/2026 17:00</td>
      <td>&#163;1,400,000.00</td>
    </tr>
    <tr>
      <td><a href="https://procontract.due-north.com/Advert?advertId=aaaa1111-bbbb-cccc-dddd-eeeeffff0002"
             title="DN817868">Environmental compliance auditing</a></td>
      <td>Defra</td>
      <td>12/06/2026 12:00</td>
      <td>N/A</td>
    </tr>
  </tbody>
</table>
"""

_LISTING_PAGE_2 = """
<table class="opportunities"><tbody>
  <tr>
    <td><a href="/Advert?advertId=aaaa1111-bbbb-cccc-dddd-eeeeffff0003" title="DN817869">
        Single-use PPE supply</a></td>
    <td>NHS North East</td>
    <td>25/05/2026 16:00</td>
    <td>£850,000.00</td>
  </tr>
</tbody></table>
"""

_EMPTY_LISTING = '<table class="opportunities"><tbody></tbody></table>'


# ---------------------------------------------------------------------------
# Listing parse
# ---------------------------------------------------------------------------


def test_parse_listing_rows_extracts_all_fields() -> None:
    rows = pd.parse_listing_rows(_LISTING_PAGE_1)
    assert len(rows) == 2

    first = rows[0]
    assert first.advert_id == "aaaa1111-bbbb-cccc-dddd-eeeeffff0001"
    assert first.dn_reference == "DN817867"
    assert first.title == "Tier 1 & 2 IT service desk"
    assert first.buyer_name == "Wiltshire Council"
    assert first.value_amount == Decimal("1400000.00")
    assert first.expression_start == datetime(2026, 5, 16, tzinfo=UTC)
    assert first.expression_end == datetime(2026, 5, 30, tzinfo=UTC)
    assert first.detail_url == pd.PROACTIS_ADVERT_URL % first.advert_id
    assert first.is_complete_for_dedup() is True


def test_parse_listing_rows_handles_absolute_href_na_value_and_single_date() -> None:
    rows = pd.parse_listing_rows(_LISTING_PAGE_1)
    second = rows[1]
    assert second.advert_id == "aaaa1111-bbbb-cccc-dddd-eeeeffff0002"
    assert second.dn_reference == "DN817868"
    assert second.buyer_name == "Defra"
    # "N/A" -> no value parsed.
    assert second.value_amount is None
    # A single date is treated as the closing/end date.
    assert second.expression_start is None
    assert second.expression_end == datetime(2026, 6, 12, tzinfo=UTC)


def test_parse_listing_rows_skips_header_and_non_opportunity_rows() -> None:
    # The <thead> row has no advert link, so it must not produce an opportunity.
    rows = pd.parse_listing_rows(_LISTING_PAGE_1)
    assert all(r.advert_id for r in rows)
    assert pd.parse_listing_rows(_EMPTY_LISTING) == []


def test_parse_listing_rows_falls_back_to_row_text_for_dn() -> None:
    """If a tenant doesn't put the DN in the link title attr, we still find it
    in the row text."""
    html = (
        '<table><tbody><tr>'
        '<td><a href="/Advert?advertId=dddd2222-0000-0000-0000-000000000001">'
        'Some opportunity</a></td>'
        '<td>Ref: DN900001</td><td>£10,000.00</td>'
        '</tr></tbody></table>'
    )
    rows = pd.parse_listing_rows(html)
    assert len(rows) == 1
    assert rows[0].dn_reference == "DN900001"


# ---------------------------------------------------------------------------
# Filter -> URL mapping
# ---------------------------------------------------------------------------


def test_build_listing_url_includes_keyword_and_paging() -> None:
    url = pd.build_listing_url(ProactisFilterConfig(keywords="cleaning"), page=2, page_size=50)
    assert url.startswith(pd.PROACTIS_OPPORTUNITIES_URL + "?")
    assert "searchKeyword=cleaning" in url
    assert "page=2" in url
    assert "pageSize=50" in url
    assert "tabName=opportunities" in url
    assert "resetFilter=True" in url


def test_build_listing_url_omits_keyword_when_empty() -> None:
    url = pd.build_listing_url(ProactisFilterConfig(keywords="   "), page=1)
    assert "searchKeyword" not in url
    # Region/category/portal/org labels are NOT URL params (see PR notes).
    url2 = pd.build_listing_url(
        ProactisFilterConfig(regions=["North West"], categories=["Cleaning"]), page=1
    )
    assert "North West" not in url2
    assert "Region" not in url2
    assert "Categor" not in url2


def test_pipeline_filter_keeps_matching_organisation_drops_others() -> None:
    opp = pd.DiscoveredOpportunity(
        advert_id="g", dn_reference="DN1234", title="Cleaning", buyer_name="Bristol City Council"
    )
    assert pd._passes_pipeline_filters(opp, ProactisFilterConfig()) is True
    assert (
        pd._passes_pipeline_filters(opp, ProactisFilterConfig(organisations=["Bristol"]))
        is True
    )
    assert (
        pd._passes_pipeline_filters(opp, ProactisFilterConfig(organisations=["Leeds"]))
        is False
    )


# ---------------------------------------------------------------------------
# Pagination walk
# ---------------------------------------------------------------------------


def _fake_fetch(pages: list[str]):
    """Return a fetch() that serves `pages` in order, then empty listings."""
    state = {"i": 0}

    async def fetch(url: str) -> str:
        i = state["i"]
        state["i"] += 1
        return pages[i] if i < len(pages) else _EMPTY_LISTING

    return fetch


async def test_walk_listing_paginates_until_short_page() -> None:
    # page_size=2: page 1 returns 2 rows (full) -> continue; page 2 returns 1
    # row (short) -> stop after it.
    fetch = _fake_fetch([_LISTING_PAGE_1, _LISTING_PAGE_2])
    rows = []
    async for row in pd._walk_listing(fetch, ProactisFilterConfig(max_pages=10), page_size=2):
        rows.append(row)
    assert pd._last_walked_pages == 2
    assert [r.advert_id[-4:] for r in rows] == ["0001", "0002", "0003"]


async def test_walk_listing_stops_on_empty_page() -> None:
    fetch = _fake_fetch([_LISTING_PAGE_2])  # 1 full-ish page then empties
    rows = []
    async for row in pd._walk_listing(fetch, ProactisFilterConfig(max_pages=10), page_size=1):
        rows.append(row)
    # page 1: 1 row (==page_size, so not short) -> continue; page 2: empty -> stop.
    assert pd._last_walked_pages == 2
    assert len(rows) == 1


async def test_walk_listing_respects_max_pages_cap() -> None:
    # One distinct, FULL page each time (page_size=1, one fresh row per page) so
    # neither the short-page nor the empty-page stop fires; only the max_pages
    # cap ends the walk, even though the fake source has unlimited pages.
    def _page(n: int) -> str:
        gid = f"cccc1111-bbbb-cccc-dddd-eeeeffff{n:04d}"
        return (
            '<table class="opportunities"><tbody>'
            f'<tr><td><a href="/Advert?advertId={gid}" title="DN9{n:05d}">Row {n}</a></td>'
            f'<td>Buyer {n}</td><td>£100,000.00</td></tr>'
            '</tbody></table>'
        )

    state = {"i": 0}

    async def fetch(url: str) -> str:
        state["i"] += 1
        return _page(state["i"])

    rows = []
    async for row in pd._walk_listing(fetch, ProactisFilterConfig(max_pages=3), page_size=1):
        rows.append(row)
    assert pd._last_walked_pages == 3
    assert len(rows) == 3


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
        detail_url=pd.PROACTIS_ADVERT_URL % advert_id,
    )


def test_upsert_routes_through_existing_upsert_tender_with_proactis_source_code() -> None:
    """The discovery service MUST funnel through `_upsert_tender`. We patch the
    name proactis_discovery imports and confirm it sees the right
    NormalisedTender shape: source_code 'PROACTIS', source_ref = advertId,
    procurement_ref = DN reference."""
    fake_tender = MagicMock()
    fake_tender.duplicate_of_id = None
    with patch.object(pd, "_upsert_tender", return_value=(fake_tender, "new")) as upsert:
        action, deduped = pd._upsert_from_discovered(MagicMock(), _make_opportunity())

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
    assert normalised.source_url == pd.PROACTIS_ADVERT_URL % "guid-1"


def test_upsert_marks_deduped_when_duplicate_of_id_set() -> None:
    """When `_upsert_tender`'s own dedup links the new row to an existing CF
    tender (sets `duplicate_of_id`), the discovery summary records it. This is
    the cross-source dedup on the shared DN reference."""
    fake_tender = MagicMock()
    fake_tender.duplicate_of_id = 42
    with patch.object(pd, "_upsert_tender", return_value=(fake_tender, "new")):
        action, deduped = pd._upsert_from_discovered(MagicMock(), _make_opportunity())
    assert action == "new"
    assert deduped is True


def test_upsert_idempotent_unchanged_returns_unchanged() -> None:
    fake_tender = MagicMock()
    fake_tender.duplicate_of_id = None
    with patch.object(pd, "_upsert_tender", return_value=(fake_tender, "unchanged")):
        action, deduped = pd._upsert_from_discovered(MagicMock(), _make_opportunity())
    assert action == "unchanged"
    assert deduped is False


# ---------------------------------------------------------------------------
# Top-level run() — end to end with fake fetch + stubbed DB
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_db_factory() -> Iterator[MagicMock]:
    """A db_factory whose context-manager session is a MagicMock. Discovery
    only needs it to (a) create a Source/PollRun (b) commit. We stub both."""
    session = MagicMock()
    existing_source = MagicMock(id=1)
    session.execute.return_value.scalar_one_or_none.return_value = existing_source
    fake_poll_run = MagicMock(id=99)
    session.get.return_value = fake_poll_run
    session.refresh = MagicMock(side_effect=lambda obj: setattr(obj, "id", 99))

    factory = MagicMock()
    factory.return_value.__enter__.return_value = session
    factory.return_value.__exit__.return_value = False
    yield factory


async def test_run_walks_pages_upserts_and_finalises(stub_db_factory) -> None:
    fetch = _fake_fetch([_LISTING_PAGE_1, _LISTING_PAGE_2])
    with patch.object(pd, "_upsert_from_discovered", return_value=("new", False)) as up:
        result = await pd.run(
            config=ProactisFilterConfig(max_pages=10),
            fetch=fetch,
            db_factory=stub_db_factory,
        )

    assert result.status == "ok"
    assert result.poll_run_id == 99
    # 3 opportunities across 2 pages (page_size default is large, so page 1 with
    # 2 rows is "short" and the walk stops — page 2 isn't fetched).
    assert result.rows_seen == 2
    assert result.opportunities_inserted == 2
    assert up.call_count == 2


async def test_run_records_error_status_on_fetch_failure(stub_db_factory) -> None:
    async def boom(url: str) -> str:
        raise RuntimeError("network down")

    result = await pd.run(
        config=ProactisFilterConfig(max_pages=3),
        fetch=boom,
        db_factory=stub_db_factory,
    )
    assert result.status == "error"
    assert "network down" in (result.error or "")
