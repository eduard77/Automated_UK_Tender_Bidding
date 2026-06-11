"""GET /admin/diagnostics/sources-health — the Phase-1 silent-source
diagnostic, offline.

In-memory SQLite stands in for Postgres; the auth dependency is overridden
the same way the other admin-diagnostic tests do. Pins the four diagnosis
classes the endpoint maps the harness's failure modes onto:

  never_polled            — Source row exists, no PollRun ever (scheduler /
                            registration class)
  fetch_failing           — newest run errored (the error string then says
                            403 vs 500 vs DNS)
  polling_but_zero_rows   — clean runs, zero tenders (the watermark-trap /
                            format class this phase fixed for EU-Supply
                            and Atamis)
  healthy                 — clean runs, rows present
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tender_agent.api.deps import current_account
from tender_agent.db import get_db
from tender_agent.main import app
from tender_agent.models import PollRun, Source, Tender
from tests._billing_fixtures import make_engine_and_session

NOW = datetime(2026, 6, 11, 12, 0, tzinfo=UTC)


@pytest.fixture()
def factory():
    _engine, factory = make_engine_and_session()
    return factory


@pytest.fixture()
def client(factory):
    def override():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    app.dependency_overrides[current_account] = lambda: object()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(current_account, None)


def _seed(factory) -> None:
    with factory() as db:
        healthy = Source(code="CF", name="Contracts Finder", base_url="x", enabled=True)
        failing = Source(code="S2W", name="Sell2Wales", base_url="x", enabled=True)
        zero = Source(code="EU_SUPPLY", name="EU-Supply", base_url="x", enabled=True)
        never = Source(code="ATAMIS", name="Atamis", base_url="x", enabled=True)
        db.add_all([healthy, failing, zero, never])
        db.commit()
        db.add_all(
            [
                # CF: clean run + a tender => healthy.
                PollRun(
                    source_id=healthy.id,
                    started_at=NOW - timedelta(minutes=30),
                    finished_at=NOW - timedelta(minutes=29),
                    status="ok",
                    fetched=3,
                    new_count=2,
                ),
                # S2W: errored run with the documented upstream message.
                PollRun(
                    source_id=failing.id,
                    started_at=NOW - timedelta(minutes=30),
                    finished_at=NOW - timedelta(minutes=29),
                    status="error",
                    error="upstream HTTP requests failed (see adapter log events)",
                ),
                # EU_SUPPLY: clean run, zero rows — the watermark trap's shape.
                PollRun(
                    source_id=zero.id,
                    started_at=NOW - timedelta(minutes=30),
                    finished_at=NOW - timedelta(minutes=29),
                    status="ok",
                    fetched=0,
                ),
                # ATAMIS: no PollRun at all.
            ]
        )
        db.add(
            Tender(
                source_code="CF",
                source_ref="cf-1",
                title="t",
                first_seen_at=NOW,
                last_seen_at=NOW,
            )
        )
        db.commit()


def test_health_classifies_all_four_diagnosis_classes(client, factory) -> None:
    _seed(factory)
    resp = client.get("/admin/diagnostics/sources-health")
    assert resp.status_code == 200
    body = resp.json()
    by_code = {s["code"]: s for s in body["sources"]}

    assert by_code["CF"]["diagnosis"] == "healthy"
    assert by_code["CF"]["tender_count"] == 1
    assert by_code["CF"]["latest_runs"][0]["status"] == "ok"

    assert by_code["S2W"]["diagnosis"] == "fetch_failing"
    assert "upstream HTTP requests failed" in (
        by_code["S2W"]["latest_runs"][0]["error"] or ""
    )

    assert by_code["EU_SUPPLY"]["diagnosis"] == "polling_but_zero_rows"
    assert by_code["EU_SUPPLY"]["tender_count"] == 0

    assert by_code["ATAMIS"]["diagnosis"] == "never_polled"
    assert by_code["ATAMIS"]["latest_runs"] == []


def test_health_runs_are_newest_first_and_capped(client, factory) -> None:
    with factory() as db:
        src = Source(code="FTS", name="FTS", base_url="x", enabled=True)
        db.add(src)
        db.commit()
        for i in range(5):
            db.add(
                PollRun(
                    source_id=src.id,
                    started_at=NOW - timedelta(hours=5 - i),
                    finished_at=NOW - timedelta(hours=5 - i, minutes=-1),
                    status="ok",
                    fetched=i,
                )
            )
        db.commit()
    body = client.get("/admin/diagnostics/sources-health").json()
    fts = next(s for s in body["sources"] if s["code"] == "FTS")
    assert len(fts["latest_runs"]) == 3  # POLL_RUNS_PER_SOURCE
    fetched = [r["fetched"] for r in fts["latest_runs"]]
    assert fetched == [4, 3, 2]  # newest first


def test_health_rejects_anonymous(factory) -> None:
    def override():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    try:
        anon = TestClient(app)
        resp = anon.get("/admin/diagnostics/sources-health")
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.pop(get_db, None)
