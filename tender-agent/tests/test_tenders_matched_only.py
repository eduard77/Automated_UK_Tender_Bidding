"""Regression tests for GET /tenders?matched_only=true.

Background (the outage these pin): the matched_only branch used
``select(Tender).join(FilterMatch).distinct()``. The join multiplies a tender
by its match count, so ``.distinct()`` was added to dedupe — but SELECT DISTINCT
makes Postgres compare WHOLE rows, and Tender carries ``json`` (not ``jsonb``)
columns (secondary_sectors, subcategories, documents, raw). The ``json`` type has
no equality operator, so Postgres raised "could not identify an equality operator
for type json" — a hard 500 isolated to this path only. The fix replaces the
join+distinct with a correlated EXISTS (no row multiplication → no DISTINCT → the
json columns are never compared).

These tests run against the REAL Postgres session (same pattern as
test_tender_search.py). That is deliberate and load-bearing: SQLite stores json
as text and DISTINCT works there, so the original bug does NOT reproduce on
SQLite — only a Postgres-backed test catches a regression to join+distinct.
"""
from __future__ import annotations

import itertools
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tender_agent.db import engine, get_db
from tender_agent.main import app
from tender_agent.models import FilterMatch, FilterProfile, Tender

MARK = "MATCHED_ONLY_TEST"  # marker source to isolate this test's controlled set
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


def _tender(session: Session, **kw) -> Tender:
    now = datetime.now(UTC)
    n = next(_counter)
    defaults = {
        "source_code": MARK,
        "source_ref": f"{MARK}-{n}",
        "title": f"Tender {n}",
        "first_seen_at": now,
        "last_seen_at": now,
        # Populate the json (NOT jsonb) columns: these are exactly what made
        # SELECT DISTINCT blow up. A realistic row keeps the test honest.
        "documents": [{"title": "ITT.pdf", "url": "https://x.test/itt", "format": "pdf"}],
        "raw": {"id": n, "nested": {"k": "v"}},
        "secondary_sectors": ["IT & Digital"],
        "subcategories": {"Construction & Built Environment": ["Refurbishment"]},
    }
    defaults.update(kw)
    t = Tender(**defaults)
    session.add(t)
    session.flush()
    return t


def _profile(session: Session, name: str = "test-profile") -> FilterProfile:
    p = FilterProfile(name=name)
    session.add(p)
    session.flush()
    return p


def _match(session: Session, tender: Tender, profile: FilterProfile) -> FilterMatch:
    m = FilterMatch(tender_id=tender.id, filter_profile_id=profile.id, score=1.0)
    session.add(m)
    session.flush()
    return m


def test_matched_only_returns_200(client: TestClient, session: Session) -> None:
    """The core regression: matched_only=true must not 500. Pre-fix this raised
    'could not identify an equality operator for type json' on Postgres."""
    matched = _tender(session)
    profile = _profile(session)
    _match(session, matched, profile)

    resp = client.get("/tenders", params={"source": MARK, "matched_only": "true"})

    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert matched.id in ids


def test_matched_only_excludes_unmatched(client: TestClient, session: Session) -> None:
    matched = _tender(session)
    unmatched = _tender(session)
    profile = _profile(session)
    _match(session, matched, profile)

    resp = client.get("/tenders", params={"source": MARK, "matched_only": "true"})

    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert matched.id in ids
    assert unmatched.id not in ids


def test_matched_only_dedupes_multi_profile_matches(
    client: TestClient, session: Session
) -> None:
    """A tender matching several profiles must appear exactly once (the EXISTS
    form gives this for free; the old join needed DISTINCT to achieve it)."""
    tender = _tender(session)
    _match(session, tender, _profile(session, name="p1"))
    _match(session, tender, _profile(session, name="p2"))
    _match(session, tender, _profile(session, name="p3"))

    resp = client.get("/tenders", params={"source": MARK, "matched_only": "true"})

    assert resp.status_code == 200, resp.text
    ids = [row["id"] for row in resp.json()]
    assert ids.count(tender.id) == 1
