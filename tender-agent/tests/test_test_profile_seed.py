"""Test FilterProfile seeder — idempotency + shape.

Drives the service function directly against an in-memory SQLite session
(via _billing_fixtures) and the admin endpoint via FastAPI's TestClient
with an overridden DB dependency. No network, no Postgres.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tender_agent.db import get_db
from tender_agent.main import app
from tender_agent.models import FilterProfile
from tender_agent.services.test_profile_seed import (
    TEST_PROFILE_CPV_PREFIXES,
    TEST_PROFILE_NAME,
    ensure_test_filter_profile,
)
from tests._billing_fixtures import make_engine_and_session


@pytest.fixture()
def factory():
    _engine, factory = make_engine_and_session()
    return factory


@pytest.fixture()
def client(factory) -> TestClient:
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


def test_first_call_creates_profile(factory) -> None:
    with factory() as db:
        profile, created = ensure_test_filter_profile(db)
    assert created is True
    assert profile.id is not None
    assert profile.name == TEST_PROFILE_NAME
    assert profile.enabled is True
    assert profile.cpv_prefixes == list(TEST_PROFILE_CPV_PREFIXES)
    # The broad-by-design rationale: regions, keywords, value narrowing all
    # unset so the test run doesn't over-filter to zero rows.
    assert profile.regions in (None, [])
    assert profile.keywords_any in (None, [])
    assert profile.value_min is None and profile.value_max is None


def test_second_call_is_idempotent(factory) -> None:
    with factory() as db:
        profile_a, created_a = ensure_test_filter_profile(db)
        profile_b, created_b = ensure_test_filter_profile(db)
        count = db.query(FilterProfile).count()
    assert created_a is True
    assert created_b is False
    assert profile_a.id == profile_b.id
    assert count == 1


def test_admin_endpoint_browser_path(client: TestClient) -> None:
    first = client.post("/admin/seed-test-profile")
    assert first.status_code == 200
    body = first.json()
    assert body["created"] is True
    assert body["name"] == TEST_PROFILE_NAME
    assert body["cpv_prefixes"] == list(TEST_PROFILE_CPV_PREFIXES)
    profile_id = body["profile_id"]
    assert isinstance(profile_id, int)

    second = client.post("/admin/seed-test-profile")
    assert second.status_code == 200
    body2 = second.json()
    assert body2["created"] is False
    assert body2["profile_id"] == profile_id


def test_seeded_profile_shows_up_in_list_filters(client: TestClient) -> None:
    """End-to-end: seed → GET /filters returns it (was [] before)."""
    client.post("/admin/seed-test-profile")
    listed = client.get("/filters").json()
    names = [row["name"] for row in listed]
    assert TEST_PROFILE_NAME in names
    seeded = next(row for row in listed if row["name"] == TEST_PROFILE_NAME)
    assert seeded["enabled"] is True
    assert seeded["cpv_prefixes"] == list(TEST_PROFILE_CPV_PREFIXES)
