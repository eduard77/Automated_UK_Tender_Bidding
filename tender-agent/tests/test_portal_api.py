"""API tests for /portals and /portal-blocklist.

Uses FastAPI TestClient + the real DB session via the dependency override.
Each test rolls back its session at teardown, so the suite is hermetic.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tender_agent.db import engine, get_db
from tender_agent.main import app
from tender_agent.models import Portal


@pytest.fixture()
def session() -> Session:
    connection = engine.connect()
    outer = connection.begin()
    session = Session(
        bind=connection, join_transaction_mode="create_savepoint"
    )
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


def _make_portal(
    session: Session,
    *,
    domain: str,
    priority: str = "medium",
    adapter_status: str = "not_started",
    tender_count: int = 0,
) -> Portal:
    now = datetime.now(UTC)
    portal = Portal(
        domain=domain,
        display_name=domain,
        priority=priority,
        adapter_status=adapter_status,
        tender_count=tender_count,
        first_seen_at=now,
        last_seen_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(portal)
    session.flush()
    return portal


def test_list_portals_returns_seed_blocklist_unaffected(
    client: TestClient, session: Session
) -> None:
    # Use a unique search token so the assertion is independent of how many
    # other portals exist in the database (CI starts empty; a dev DB may hold
    # thousands, which would otherwise push low-priority rows past the limit).
    _make_portal(session, domain="alpha-uniq42.example.com", priority="high", tender_count=5)
    _make_portal(session, domain="beta-uniq42.example.com", priority="low", tender_count=1)
    session.flush()
    resp = client.get("/portals?search=uniq42&limit=100")
    assert resp.status_code == 200
    domains = [p["domain"] for p in resp.json()]
    assert "alpha-uniq42.example.com" in domains
    assert "beta-uniq42.example.com" in domains


def test_list_portals_filters_by_priority(client: TestClient, session: Session) -> None:
    _make_portal(session, domain="crit.example.com", priority="critical")
    _make_portal(session, domain="low.example.com", priority="low")
    session.flush()
    resp = client.get("/portals?priority=critical")
    assert resp.status_code == 200
    payload = resp.json()
    domains = [p["domain"] for p in payload]
    assert "crit.example.com" in domains
    assert "low.example.com" not in domains


def test_list_portals_search(client: TestClient, session: Session) -> None:
    _make_portal(session, domain="needle.example.com")
    _make_portal(session, domain="haystack.example.org")
    session.flush()
    resp = client.get("/portals?search=needle")
    assert resp.status_code == 200
    domains = [p["domain"] for p in resp.json()]
    assert domains == ["needle.example.com"]


def test_patch_portal_updates_fields(client: TestClient, session: Session) -> None:
    portal = _make_portal(session, domain="patchme.example.com")
    session.flush()
    resp = client.patch(
        f"/portals/{portal.id}",
        json={
            "display_name": "Patched Portal",
            "priority": "high",
            "login_type": "username_password",
            "adapter_status": "stub",
            "notes": "Reviewed manually.",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["display_name"] == "Patched Portal"
    assert body["priority"] == "high"
    assert body["login_type"] == "username_password"
    assert body["adapter_status"] == "stub"
    assert body["notes"] == "Reviewed manually."


def test_delete_portal_is_soft_delete(client: TestClient, session: Session) -> None:
    portal = _make_portal(session, domain="deprecate.example.com")
    session.flush()
    resp = client.delete(f"/portals/{portal.id}")
    assert resp.status_code == 200
    assert resp.json()["adapter_status"] == "deprecated"
    # Row still present
    session.expire_all()
    still_there = session.get(Portal, portal.id)
    assert still_there is not None
    assert still_there.adapter_status == "deprecated"


def test_reclassify_endpoint_schedules(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, session: Session
) -> None:
    portal = _make_portal(session, domain="reclassify.example.com")
    session.flush()
    seen: list[int] = []
    monkeypatch.setattr(
        "tender_agent.api.portals.schedule_classification",
        lambda pid: seen.append(pid),
    )
    resp = client.post(f"/portals/{portal.id}/classify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["queued"] is True
    # BackgroundTasks runs after the response — the test client drains them
    # synchronously, so we expect the hook to have been called.
    assert seen == [portal.id]


def test_blocklist_crud(client: TestClient) -> None:
    list_resp = client.get("/portal-blocklist")
    assert list_resp.status_code == 200
    seeded_count = len(list_resp.json())
    assert seeded_count >= 10  # at least the system seeds from migration 0005

    add_resp = client.post(
        "/portal-blocklist",
        json={"domain": "TestBlock.example.com ", "reason": "test"},
    )
    assert add_resp.status_code == 201
    new_id = add_resp.json()["id"]
    assert add_resp.json()["domain"] == "testblock.example.com"
    assert add_resp.json()["added_by"] == "user"

    dup_resp = client.post(
        "/portal-blocklist",
        json={"domain": "testblock.example.com", "reason": "dup"},
    )
    assert dup_resp.status_code == 409

    del_resp = client.delete(f"/portal-blocklist/{new_id}")
    assert del_resp.status_code == 204

    after_resp = client.get("/portal-blocklist")
    assert after_resp.status_code == 200
    assert len(after_resp.json()) == seeded_count
