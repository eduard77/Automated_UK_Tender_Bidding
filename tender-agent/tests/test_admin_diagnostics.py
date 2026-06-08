"""Tests for GET /admin/diagnostics/cf-onward-routes.

The classifier itself is unit-tested in test_onward_routes.py — here we only
verify the API wrapper: auth gating (401 anonymous) and that, given seeded
tenders covering each bucket, the endpoint returns the right counts, the
per-portal direct-link breakdown, and the sample.

Mirrors the real-DB + savepoint-rollback pattern from test_delta_session_admin.py.
Tenders are seeded under a throwaway source code and scanned via the `sources`
query param so the assertions don't depend on whatever CF/FTS rows already
exist in the test database.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tender_agent.db import engine, get_db
from tender_agent.main import app
from tender_agent.models import Tender
from tests._auth_helpers import authenticate_unlimited

SOURCE = "DIAGTEST"  # isolate from any real CF/FTS rows in the test DB


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
def anon_client(session) -> TestClient:
    def override():
        yield session

    app.dependency_overrides[get_db] = override
    tc = TestClient(app)
    try:
        yield tc
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture()
def auth_client(session) -> TestClient:
    def override():
        yield session

    app.dependency_overrides[get_db] = override
    tc = TestClient(app)
    try:
        authenticate_unlimited(tc)
        yield tc
    finally:
        app.dependency_overrides.pop(get_db, None)


def _seed(session: Session) -> None:
    """Four tenders, one per bucket (the direct one routes to Delta)."""
    now = datetime.now(UTC)
    rows = [
        Tender(
            source_code=SOURCE,
            source_ref="diag-direct",
            title="Direct portal link",
            description=(
                "Respond on Delta: "
                "https://www.delta-esourcing.com/respond/AB12CD34EF?accessCode=AB12CD34EF"
            ),
            first_seen_at=now,
            last_seen_at=now,
        ),
        Tender(
            source_code=SOURCE,
            source_ref="diag-generic",
            title="Generic portal link",
            description="Register interest on the portal: https://www.delta-esourcing.com/",
            first_seen_at=now,
            last_seen_at=now,
        ),
        Tender(
            source_code=SOURCE,
            source_ref="diag-email",
            title="Email only",
            description="To express interest email procurement@example-council.gov.uk.",
            first_seen_at=now,
            last_seen_at=now,
        ),
        Tender(
            source_code=SOURCE,
            source_ref="diag-none",
            title="Nothing actionable",
            description="Catering supplies — small-value notice. No further details.",
            first_seen_at=now,
            last_seen_at=now,
        ),
    ]
    session.add_all(rows)
    session.commit()


def test_endpoint_rejects_anonymous(anon_client):
    assert (
        anon_client.get("/admin/diagnostics/cf-onward-routes").status_code == 401
    )


def test_survey_returns_bucket_counts_and_breakdown(auth_client, session):
    _seed(session)

    r = auth_client.get(f"/admin/diagnostics/cf-onward-routes?sources={SOURCE}")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["total_scanned"] == 4
    assert body["sources"] == [SOURCE]
    assert body["buckets"] == {
        "direct_portal_link": 1,
        "portal_generic_link": 1,
        "email_only": 1,
        "none": 1,
    }
    assert body["direct_portal_breakdown"] == [{"portal": "delta", "count": 1}]

    # Sample echoes back one row per seeded tender (<= SAMPLE_SIZE).
    assert len(body["sample"]) == 4
    by_bucket = {row["bucket"]: row for row in body["sample"]}
    assert by_bucket["direct_portal_link"]["portal"] == "delta"
    assert "delta-esourcing.com" in by_bucket["direct_portal_link"]["detail"]
    assert by_bucket["email_only"]["detail"] == "procurement@example-council.gov.uk"
    assert by_bucket["none"]["portal"] is None


def test_limit_caps_the_scan(auth_client, session):
    _seed(session)

    r = auth_client.get(
        f"/admin/diagnostics/cf-onward-routes?sources={SOURCE}&limit=2"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_scanned"] == 2
    assert sum(body["buckets"].values()) == 2
