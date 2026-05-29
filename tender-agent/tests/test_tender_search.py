"""Search/filter over the canonical tender table (Phase 4 chunk 7).

These tests run against the real Postgres session (array overlap, array_to_string,
trigram ILIKE are all Postgres features). The table already holds production
rows, so every test scopes its query to a unique marker source_code and asserts
on that controlled subset — that isolates the filter-under-test from whatever
else is in the DB while still exercising the real query path.
"""
from __future__ import annotations

import itertools
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tender_agent.db import engine, get_db
from tender_agent.main import app
from tender_agent.models import Tender
from tender_agent.services.tender_search import (
    SortKey,
    TenderSearchParams,
    search_tenders,
)

MARK = "SRCH_TEST"  # marker source for the controlled set in most tests
_counter = itertools.count(1)


@pytest.fixture()
def session() -> Session:
    connection = engine.connect()
    outer = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        connection.close()


@pytest.fixture()
def client(session: Session) -> TestClient:
    def override() -> Session:
        yield session

    app.dependency_overrides[get_db] = override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _t(session: Session, *, source: str = MARK, **kw) -> Tender:
    now = datetime.now(UTC)
    n = next(_counter)
    defaults = {
        "source_code": source,
        "source_ref": f"{source}-{n}",
        "title": kw.pop("title", f"Tender {n}"),
        "first_seen_at": now,
        "last_seen_at": now,
    }
    defaults.update(kw)
    t = Tender(**defaults)
    session.add(t)
    session.flush()
    return t


def _ids(rows: list[Tender]) -> set[int]:
    return {t.id for t in rows}


# ---------------------------------------------------------------------------
# Each filter in isolation
# ---------------------------------------------------------------------------

def test_q_matches_title_description_and_keywords(session: Session) -> None:
    token = "zqxsearchtoken"
    in_title = _t(session, title=f"Road {token} works")
    in_desc = _t(session, description=f"includes {token} clause")
    in_kw = _t(session, keywords=["misc", token, "other"])
    nomatch = _t(session, title="unrelated", description="nothing", keywords=["x"])

    total, rows = search_tenders(session, TenderSearchParams(q=token, source=[MARK]))
    assert _ids(rows) == {in_title.id, in_desc.id, in_kw.id}
    assert nomatch.id not in _ids(rows)
    assert total == 3


def test_q_is_case_insensitive(session: Session) -> None:
    hit = _t(session, title="Major BRIDGE Refurbishment")
    _t(session, title="something else")
    _, rows = search_tenders(session, TenderSearchParams(q="bridge", source=[MARK]))
    assert _ids(rows) == {hit.id}


def test_cpv_overlap(session: Session) -> None:
    a = _t(session, cpv_codes=["45000000", "45210000"])
    b = _t(session, cpv_codes=["72000000"])
    c = _t(session, cpv_codes=None)
    _, rows = search_tenders(
        session, TenderSearchParams(cpv=["45000000"], source=[MARK])
    )
    assert _ids(rows) == {a.id}
    assert b.id not in _ids(rows) and c.id not in _ids(rows)


def test_region_exact_match_on_canonical_column(session: Session) -> None:
    # Region now filters the canonical `region` column by EXACT value (chunk 8),
    # not buyer_region free text. A tender resolved to North West (e.g. via a
    # postcode) is found by selecting "North West".
    nw = _t(session, region="North West", buyer_region="Manchester")
    london = _t(session, region="London", buyer_region="Westminster")
    sw = _t(session, region="South West", buyer_region="Bristol")
    # buyer_region city text must NOT match the region filter.
    _, rows = search_tenders(
        session, TenderSearchParams(region=["North West"], source=[MARK])
    )
    assert _ids(rows) == {nw.id}
    assert london.id not in _ids(rows) and sw.id not in _ids(rows)
    # Multi-select is OR across canonical values.
    _, rows2 = search_tenders(
        session, TenderSearchParams(region=["North West", "London"], source=[MARK])
    )
    assert _ids(rows2) == {nw.id, london.id}


def test_buyer_contains(session: Session) -> None:
    a = _t(session, buyer_name="Alpha Borough Council")
    _t(session, buyer_name="Beta Trust")
    _, rows = search_tenders(
        session, TenderSearchParams(buyer="alpha", source=[MARK])
    )
    assert _ids(rows) == {a.id}


def test_value_range_includes_null_value_by_default(session: Session) -> None:
    low = _t(session, value_amount=Decimal("50000"))
    mid = _t(session, value_amount=Decimal("200000"))
    band = _t(session, value_min=Decimal("100000"), value_max=Decimal("300000"))
    novalue = _t(session)  # no value at all

    _, rows = search_tenders(
        session,
        TenderSearchParams(
            value_min=Decimal("150000"), value_max=Decimal("250000"), source=[MARK]
        ),
    )
    # In-range tenders AND null-value tenders are included by default (chunk 8):
    # a value range must never silently drop the large null-value population.
    assert _ids(rows) == {mid.id, band.id, novalue.id}
    assert low.id not in _ids(rows)


def test_value_stated_only_toggle_excludes_null_value(session: Session) -> None:
    mid = _t(session, value_amount=Decimal("200000"))
    band = _t(session, value_min=Decimal("100000"), value_max=Decimal("300000"))
    novalue = _t(session)

    _, rows = search_tenders(
        session,
        TenderSearchParams(
            value_min=Decimal("150000"),
            value_max=Decimal("250000"),
            value_stated_only=True,
            source=[MARK],
        ),
    )
    assert _ids(rows) == {mid.id, band.id}
    assert novalue.id not in _ids(rows)


def test_value_min_only_includes_null_value_by_default(session: Session) -> None:
    hit = _t(session, value_amount=Decimal("500000"))
    low = _t(session, value_amount=Decimal("1000"))
    novalue = _t(session)  # null value
    _, rows = search_tenders(
        session, TenderSearchParams(value_min=Decimal("100000"), source=[MARK])
    )
    # The "set a value → everything with no value vanishes" failure must not
    # recur: null-value tenders stay in unless the toggle is set.
    assert hit.id in _ids(rows)
    assert novalue.id in _ids(rows)
    assert low.id not in _ids(rows)
    # Toggle on → nulls excluded.
    _, rows2 = search_tenders(
        session,
        TenderSearchParams(
            value_min=Decimal("100000"), value_stated_only=True, source=[MARK]
        ),
    )
    assert _ids(rows2) == {hit.id}


def test_deadline_window(session: Session) -> None:
    early = _t(session, deadline_at=datetime(2026, 6, 1, tzinfo=UTC))
    inside = _t(session, deadline_at=datetime(2026, 7, 1, tzinfo=UTC))
    _t(session, deadline_at=None)
    _, rows = search_tenders(
        session,
        TenderSearchParams(
            deadline_from=datetime(2026, 6, 15, tzinfo=UTC),
            deadline_to=datetime(2026, 7, 15, tzinfo=UTC),
            source=[MARK],
        ),
    )
    assert _ids(rows) == {inside.id}
    assert early.id not in _ids(rows)


def test_open_only(session: Session) -> None:
    open_live = _t(
        session, status="active", deadline_at=datetime(2099, 1, 1, tzinfo=UTC)
    )
    _t(session, status="active", deadline_at=datetime(2000, 1, 1, tzinfo=UTC))  # past
    _t(session, status="closed", deadline_at=datetime(2099, 1, 1, tzinfo=UTC))  # closed
    _t(session, status="planned", deadline_at=datetime(2099, 1, 1, tzinfo=UTC))
    _, rows = search_tenders(
        session, TenderSearchParams(open_only=True, source=[MARK])
    )
    assert _ids(rows) == {open_live.id}


def test_status_filter(session: Session) -> None:
    a = _t(session, status="active")
    _t(session, status="closed")
    p = _t(session, status="planned")
    _, rows = search_tenders(
        session, TenderSearchParams(status=["active", "planned"], source=[MARK])
    )
    assert _ids(rows) == {a.id, p.id}


def test_source_filter(session: Session) -> None:
    a = _t(session, source=MARK)
    b = _t(session, source="SRCH_TEST2")
    _, rows = search_tenders(
        session, TenderSearchParams(source=["SRCH_TEST2"])
    )
    assert b.id in _ids(rows)
    assert a.id not in _ids(rows)


# ---------------------------------------------------------------------------
# Combined filters AND
# ---------------------------------------------------------------------------

def test_combined_filters_are_anded(session: Session) -> None:
    token = "combotoken"
    both = _t(session, title=f"{token} job", region="North West")
    only_q = _t(session, title=f"{token} job", region="London")
    only_region = _t(session, title="plain", region="North West")
    _, rows = search_tenders(
        session,
        TenderSearchParams(q=token, region=["North West"], source=[MARK]),
    )
    assert _ids(rows) == {both.id}
    assert only_q.id not in _ids(rows) and only_region.id not in _ids(rows)


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------

def test_include_duplicates_true_returns_all(session: Session) -> None:
    primary = _t(session, title="primary notice")
    dup = _t(session, title="dup notice", duplicate_of_id=primary.id)
    total, rows = search_tenders(
        session, TenderSearchParams(include_duplicates=True, source=[MARK])
    )
    assert {primary.id, dup.id} <= _ids(rows)
    assert total >= 2


def test_include_duplicates_false_returns_primaries_only(session: Session) -> None:
    primary = _t(session, title="primary notice")
    dup = _t(session, title="dup notice", duplicate_of_id=primary.id)
    _, rows = search_tenders(
        session, TenderSearchParams(include_duplicates=False, source=[MARK])
    )
    assert primary.id in _ids(rows)
    assert dup.id not in _ids(rows)


def test_dedup_annotation_references_primary(client: TestClient, session: Session) -> None:
    primary = _t(session, title="primary notice")
    dup = _t(session, title="dup notice", duplicate_of_id=primary.id)
    session.flush()
    resp = client.get(
        "/tenders/search", params={"source": MARK, "include_duplicates": "true"}
    )
    assert resp.status_code == 200
    by_id = {r["id"]: r for r in resp.json()["results"]}
    assert by_id[dup.id]["is_duplicate"] is True
    assert by_id[dup.id]["duplicate_of_id"] == primary.id
    assert by_id[primary.id]["is_duplicate"] is False


# ---------------------------------------------------------------------------
# Pagination + total + sort
# ---------------------------------------------------------------------------

def test_pagination_and_total(session: Session) -> None:
    src = "SRCH_PAGE"
    made = [_t(session, source=src) for _ in range(5)]
    made_ids = {t.id for t in made}

    total, p1 = search_tenders(
        session, TenderSearchParams(source=[src], page=1, page_size=2)
    )
    _, p2 = search_tenders(
        session, TenderSearchParams(source=[src], page=2, page_size=2)
    )
    _, p3 = search_tenders(
        session, TenderSearchParams(source=[src], page=3, page_size=2)
    )
    assert total == 5
    assert len(p1) == 2 and len(p2) == 2 and len(p3) == 1
    # Pages are disjoint and together cover the full set.
    assert _ids(p1) | _ids(p2) | _ids(p3) == made_ids
    assert _ids(p1).isdisjoint(_ids(p2))


def test_page_size_capped_at_100(session: Session) -> None:
    _, rows = search_tenders(session, TenderSearchParams(page_size=10_000))
    assert len(rows) <= 100


def test_sort_deadline_asc(session: Session) -> None:
    src = "SRCH_SORT_D"
    t_late = _t(session, source=src, deadline_at=datetime(2030, 3, 1, tzinfo=UTC))
    t_early = _t(session, source=src, deadline_at=datetime(2030, 1, 1, tzinfo=UTC))
    t_mid = _t(session, source=src, deadline_at=datetime(2030, 2, 1, tzinfo=UTC))
    t_none = _t(session, source=src, deadline_at=None)
    _, rows = search_tenders(
        session, TenderSearchParams(source=[src], sort=SortKey.deadline_asc)
    )
    order = [t.id for t in rows]
    assert order[:3] == [t_early.id, t_mid.id, t_late.id]
    assert order[-1] == t_none.id  # null deadline last


def test_sort_value_desc(session: Session) -> None:
    src = "SRCH_SORT_V"
    small = _t(session, source=src, value_amount=Decimal("1000"))
    big = _t(session, source=src, value_amount=Decimal("9000000"))
    mid = _t(session, source=src, value_amount=Decimal("50000"))
    _, rows = search_tenders(
        session, TenderSearchParams(source=[src], sort=SortKey.value_desc)
    )
    assert [t.id for t in rows][:3] == [big.id, mid.id, small.id]


def test_sort_published_desc(session: Session) -> None:
    src = "SRCH_SORT_P"
    older = _t(session, source=src, published_at=datetime(2026, 1, 1, tzinfo=UTC))
    newer = _t(session, source=src, published_at=datetime(2026, 5, 1, tzinfo=UTC))
    _, rows = search_tenders(
        session, TenderSearchParams(source=[src], sort=SortKey.published_desc)
    )
    assert [t.id for t in rows][:2] == [newer.id, older.id]


# ---------------------------------------------------------------------------
# Source-agnostic guarantee
# ---------------------------------------------------------------------------

def test_new_source_is_searchable_with_no_code_change(session: Session) -> None:
    """Insert a tender under a source_code that did not exist before — it must
    appear in search and be filterable by that source, with zero code change."""
    future = _t(session, source="FUTURE_PORTAL", title="brand new source tender")
    total, rows = search_tenders(
        session, TenderSearchParams(source=["FUTURE_PORTAL"])
    )
    assert _ids(rows) == {future.id}
    assert total == 1
    # And it is reachable by a generic free-text search too.
    _, rows2 = search_tenders(
        session, TenderSearchParams(q="brand new source", source=["FUTURE_PORTAL"])
    )
    assert _ids(rows2) == {future.id}


# ---------------------------------------------------------------------------
# API surface (routing + response shape)
# ---------------------------------------------------------------------------

def test_search_endpoint_shape_and_routing(client: TestClient, session: Session) -> None:
    _t(session, title="api shape tender")
    session.flush()
    resp = client.get("/tenders/search", params={"source": MARK, "page_size": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"total", "page", "page_size", "results"}
    assert body["page"] == 1 and body["page_size"] == 5
    if body["results"]:
        r = body["results"][0]
        for key in ("id", "title", "source_code", "is_duplicate", "duplicate_of_id"):
            assert key in r


def test_facets_endpoint(client: TestClient, session: Session) -> None:
    _t(session, source="FACET_SRC", status="active", region="North West")
    _t(session, source="FACET_SRC", status="active", region="North West")
    _t(session, source="FACET_SRC", status="active", region="London")
    session.flush()
    resp = client.get("/tenders/facets")
    assert resp.status_code == 200
    body = resp.json()
    assert "FACET_SRC" in body["sources"]
    assert "active" in body["statuses"]
    # Regions are canonical values with counts (chunk 8), ordered by the
    # canonical list (North West before London).
    by_region = {r["value"]: r["count"] for r in body["regions"]}
    assert by_region.get("North West", 0) >= 2
    assert by_region.get("London", 0) >= 1
    values = [r["value"] for r in body["regions"]]
    assert values.index("North West") < values.index("London")


def test_search_route_not_shadowed_by_detail_route(client: TestClient) -> None:
    """/tenders/search must resolve to the search handler, not /tenders/{id}."""
    resp = client.get("/tenders/search")
    assert resp.status_code == 200
    assert "results" in resp.json()
