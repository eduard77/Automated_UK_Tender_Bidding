"""Credentials API against the cloud-safe store backend — fully offline.

In-memory SQLite stands in for Postgres (via _billing_fixtures); the store
singleton is swapped for one bound to the same DB with a throwaway key.
Proves the POST -> stored -> listed flow works on the new backend, that the
password never appears in any response body, and that a missing key surfaces
as a 503 whose detail names the CREDENTIALS_ENCRYPTION_KEY app setting.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from tender_agent.db import get_db
from tender_agent.main import app
from tender_agent.models import Portal
from tender_agent.services import credentials as creds_mod
from tests._billing_fixtures import make_engine_and_session


@pytest.fixture()
def factory():
    _engine, factory = make_engine_and_session()
    return factory


@pytest.fixture()
def client(factory, monkeypatch) -> TestClient:
    store = creds_mod.CredentialsStore(
        session_factory=factory, encryption_key=Fernet.generate_key().decode()
    )
    monkeypatch.setattr(creds_mod, "_store", store)

    def override():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _make_portal(factory) -> int:
    now = datetime.now(UTC)
    with factory() as db:
        portal = Portal(
            domain="procontract.due-north.com",
            display_name="Proactis",
            priority="high",
            adapter_status="not_started",
            tender_count=0,
            first_seen_at=now,
            last_seen_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(portal)
        db.commit()
        return portal.id


def test_post_then_list_roundtrip_without_password_leak(
    client: TestClient, factory
) -> None:
    portal_id = _make_portal(factory)
    resp = client.post(
        "/credentials",
        json={
            "portal_id": portal_id,
            "username": "ops@example.com",
            "password": "Sup3rSecret!",
        },
    )
    assert resp.status_code == 201
    assert "Sup3rSecret!" not in resp.text

    listed = client.get("/credentials")
    assert listed.status_code == 200
    body = listed.json()
    assert [m["portal_id"] for m in body] == [portal_id]
    assert "Sup3rSecret!" not in listed.text

    # And the store actually decrypts it back for internal use.
    got = creds_mod.get_store().get_credentials(portal_id, "eduard")
    assert got is not None and got.password == "Sup3rSecret!"


def test_post_without_key_returns_503_naming_the_app_setting(
    client: TestClient, factory, monkeypatch
) -> None:
    portal_id = _make_portal(factory)
    # Swap in a store with NO key from any source (env empty, no keyring).
    from tender_agent.config import settings

    monkeypatch.setattr(settings, "credentials_encryption_key", "")
    monkeypatch.setattr(
        creds_mod.CredentialsStore, "_key_from_keyring", lambda self: None
    )
    monkeypatch.setattr(
        creds_mod, "_store", creds_mod.CredentialsStore(session_factory=factory)
    )
    resp = client.post(
        "/credentials",
        json={
            "portal_id": portal_id,
            "username": "ops@example.com",
            "password": "Sup3rSecret!",
        },
    )
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "CREDENTIALS_ENCRYPTION_KEY" in detail
    assert "requires OS keyring" not in detail
    assert "Sup3rSecret!" not in resp.text
