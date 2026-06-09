"""Push subscribe endpoint — now authenticated and account-owned."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tender_agent.config import settings
from tender_agent.db import get_db
from tender_agent.main import app
from tender_agent.models import Account, PushSubscription
from tender_agent.services.accounts import passwords
from tests._auth_helpers import authenticate_unlimited
from tests._billing_fixtures import make_engine_and_session


@pytest.fixture(autouse=True)
def _fast_bcrypt(monkeypatch) -> None:
    monkeypatch.setattr(passwords, "_ROUNDS", 4)


@pytest.fixture(autouse=True)
def _push_configured(monkeypatch) -> None:
    monkeypatch.setattr(settings, "vapid_public_key", "pub")
    monkeypatch.setattr(settings, "vapid_private_key", "priv")


@pytest.fixture()
def db_factory():
    _, factory = make_engine_and_session()
    return factory


@pytest.fixture()
def client(db_factory):
    def _override():
        s = db_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def db(db_factory):
    s = db_factory()
    try:
        yield s
    finally:
        s.close()


_SUB_BODY = {
    "endpoint": "https://push.example/ep-1",
    "keys": {"p256dh": "p", "auth": "a"},
    "filter_profile_id": None,
}


def test_subscribe_requires_auth(client) -> None:
    assert client.post("/push/subscriptions", json=_SUB_BODY).status_code == 401


def test_subscribe_records_the_authenticated_account(client, db) -> None:
    authenticate_unlimited(client)
    me = db.execute(select(Account)).scalars().one()

    r = client.post("/push/subscriptions", json=_SUB_BODY)
    assert r.status_code == 201
    assert r.json()["account_id"] == me.id

    sub = db.execute(select(PushSubscription)).scalars().one()
    assert sub.account_id == me.id
    assert sub.endpoint == _SUB_BODY["endpoint"]
