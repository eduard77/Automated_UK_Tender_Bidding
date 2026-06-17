"""Phase 3b — the sector taxonomy endpoint + per-user saved sector preferences.

In-memory SQLite (no Postgres needed): these endpoints touch the taxonomy and
the accounts table only, none of the Postgres-only array/JSONB search features.

The saved-sectors routes are STRICTLY per-user — every test that writes for one
account proves another account can't see or overwrite it. `current_account` is
overridden with a dependency that loads the account from the SAME request
session the endpoint commits on (mirrors production, where require_account
resolves the account on the request's db session).
"""
from __future__ import annotations

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tender_agent.api.deps import current_account
from tender_agent.db import get_db
from tender_agent.main import app
from tender_agent.models import Account
from tender_agent.services.classification.taxonomy import SECTORS
from tests._billing_fixtures import make_account, make_engine_and_session


@pytest.fixture()
def factory():
    _engine, factory = make_engine_and_session()
    return factory


@pytest.fixture()
def db_override(factory):
    def override():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(current_account, None)


def _as_account(account_id: int):
    """Override current_account to load the account on the REQUEST's session,
    so the endpoint's db.commit() persists the change (as in production)."""

    def _override(db: Session = Depends(get_db)) -> Account | None:  # noqa: B008
        return db.get(Account, account_id)

    return _override


# ---------------------------------------------------------------------------
# Taxonomy endpoint
# ---------------------------------------------------------------------------


def test_sectors_endpoint_returns_canonical_taxonomy(db_override) -> None:
    client = TestClient(app)
    resp = client.get("/tenders/sectors")
    assert resp.status_code == 200
    body = resp.json()
    # Single source of truth: exactly the taxonomy's 16 sectors, in order.
    assert body["sectors"] == list(SECTORS)
    assert len(body["sectors"]) == 16
    assert "Construction & Built Environment" in body["sectors"]
    assert "Other / Uncategorised" in body["sectors"]


# ---------------------------------------------------------------------------
# Saved sectors — round-trip, validation, per-user isolation
# ---------------------------------------------------------------------------


def test_saved_sectors_empty_by_default(factory, db_override) -> None:
    with factory() as db:
        account = make_account(db, email="a@example.com")
        aid = account.id
    app.dependency_overrides[current_account] = _as_account(aid)

    resp = TestClient(app).get("/me/sectors")
    assert resp.status_code == 200
    assert resp.json() == {"sectors": []}


def test_saved_sectors_round_trip_with_canonicalisation(factory, db_override) -> None:
    with factory() as db:
        account = make_account(db, email="a@example.com")
        aid = account.id
    app.dependency_overrides[current_account] = _as_account(aid)
    client = TestClient(app)

    # Junk is dropped, case-drift is canonicalised, duplicates collapse.
    resp = client.put(
        "/me/sectors",
        json={
            "sectors": [
                "it & digital",  # case-drift → canonical "IT & Digital"
                "IT & Digital",  # duplicate
                "Not A Real Sector",  # junk → dropped
                "Health & Social Care",
            ]
        },
    )
    assert resp.status_code == 200
    assert resp.json()["sectors"] == ["IT & Digital", "Health & Social Care"]

    # Persisted: a fresh GET returns the stored canonical list.
    assert client.get("/me/sectors").json()["sectors"] == [
        "IT & Digital",
        "Health & Social Care",
    ]


def test_saved_sectors_are_strictly_per_user(factory, db_override) -> None:
    with factory() as db:
        a = make_account(db, email="a@example.com")
        b = make_account(db, email="b@example.com")
        a_id, b_id = a.id, b.id

    # Account A saves a selection.
    app.dependency_overrides[current_account] = _as_account(a_id)
    TestClient(app).put(
        "/me/sectors", json={"sectors": ["IT & Digital"]}
    ).raise_for_status()

    # Account B sees NOTHING of A's — no bleed across users.
    app.dependency_overrides[current_account] = _as_account(b_id)
    assert TestClient(app).get("/me/sectors").json() == {"sectors": []}

    # B saves its own, distinct selection.
    TestClient(app).put(
        "/me/sectors", json={"sectors": ["Construction & Built Environment"]}
    ).raise_for_status()

    # A is unchanged — B's write didn't touch A.
    app.dependency_overrides[current_account] = _as_account(a_id)
    assert TestClient(app).get("/me/sectors").json() == {
        "sectors": ["IT & Digital"]
    }
    # And B kept its own.
    app.dependency_overrides[current_account] = _as_account(b_id)
    assert TestClient(app).get("/me/sectors").json() == {
        "sectors": ["Construction & Built Environment"]
    }


def test_saved_sectors_require_authentication(db_override) -> None:
    # No current_account override → anonymous → 401 on both read and write.
    client = TestClient(app)
    assert client.get("/me/sectors").status_code == 401
    assert (
        client.put("/me/sectors", json={"sectors": ["IT & Digital"]}).status_code
        == 401
    )
