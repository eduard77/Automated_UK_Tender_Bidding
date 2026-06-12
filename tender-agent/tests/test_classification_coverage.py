"""GET /admin/diagnostics/classification-coverage — the Phase-3a gate readout.

Counts tenders by primary_sector and per-source classified coverage, so the
operator can paste it back as evidence that AI sector classification populated
across ALL sources. In-memory SQLite; auth overridden like the other
admin-diagnostic tests.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from tender_agent.api.deps import current_account
from tender_agent.db import get_db
from tender_agent.main import app
from tender_agent.models import Tender
from tender_agent.services.classification.taxonomy import CLASSIFIER_VERSION
from tests._billing_fixtures import make_engine_and_session

NOW = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)


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


def _classified(source_code, ref, sector) -> Tender:
    return Tender(
        source_code=source_code,
        source_ref=ref,
        title="t",
        primary_sector=sector,
        classifier_version=CLASSIFIER_VERSION,
        classified_at=NOW,
        first_seen_at=NOW,
        last_seen_at=NOW,
    )


def _unclassified(source_code, ref) -> Tender:
    return Tender(
        source_code=source_code,
        source_ref=ref,
        title="t",
        first_seen_at=NOW,
        last_seen_at=NOW,
    )


def test_coverage_counts_by_sector_and_source(client, factory) -> None:
    with factory() as db:
        db.add_all(
            [
                _classified("CF", "c1", "Construction & Built Environment"),
                _classified("CF", "c2", "IT & Digital"),
                _unclassified("CF", "c3"),
                # The formerly-silent source now carries a sector.
                _classified("PROACTIS", "p1", "Construction & Built Environment"),
                _classified("ATAMIS", "a1", "Other / Uncategorised"),
            ]
        )
        db.commit()

    resp = client.get("/admin/diagnostics/classification-coverage")
    assert resp.status_code == 200
    body = resp.json()

    assert body["classifier_version"] == CLASSIFIER_VERSION
    assert body["total_tenders"] == 5
    assert body["classified"] == 4
    assert body["unclassified"] == 1

    by_sector = {s["sector"]: s["count"] for s in body["by_primary_sector"]}
    assert by_sector["Construction & Built Environment"] == 2
    assert by_sector["IT & Digital"] == 1
    assert by_sector["Other / Uncategorised"] == 1
    assert by_sector[None] == 1  # the unclassified CF row

    by_source = {s["source_code"]: s for s in body["by_source"]}
    assert by_source["CF"]["total"] == 3 and by_source["CF"]["classified"] == 2
    assert by_source["PROACTIS"]["classified"] == 1
    assert by_source["ATAMIS"]["classified"] == 1


def test_coverage_rejects_anonymous(factory) -> None:
    def override():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    try:
        anon = TestClient(app)
        resp = anon.get("/admin/diagnostics/classification-coverage")
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.pop(get_db, None)
