"""API tests for /tenders/{id}/go-no-go and /tenders/{id}/vault-reconciliation."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from tender_agent.db import get_db
from tender_agent.main import app
from tender_agent.models import (
    Tender,
    TenderBrief,
    VaultDocument,
    VaultDocumentVersion,
)
from tests._billing_fixtures import make_engine_and_session


@pytest.fixture()
def db_factory():
    _, factory = make_engine_and_session()
    return factory


@pytest.fixture()
def client(db_factory):
    def override():
        s = db_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def db(db_factory):
    s = db_factory()
    try:
        yield s
    finally:
        s.close()


def _make_tender(db, *, deadline_at: datetime | None = None) -> Tender:
    t = Tender(
        source_code="TEST",
        source_ref=f"api-{datetime.now().timestamp()}",
        title="API tender",
        value_amount=Decimal(200_000),
        deadline_at=deadline_at,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _make_brief(db, *, tender_id: int, mandatory: list[str] | None = None) -> TenderBrief:
    b = TenderBrief(
        tender_id=tender_id,
        status="complete",
        recommendation="bid",
        confidence="medium",
        brief_json={
            "recommendation": "bid",
            "confidence": "medium",
            "headline": "h",
            "rationale": "r",
            "mandatory_requirements": mandatory or [],
            "scope_summary": "scope",
        },
        generated_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


def test_go_no_go_returns_full_shape(client, db) -> None:
    t = _make_tender(db, deadline_at=datetime.now(UTC) + timedelta(days=60))
    _make_brief(db, tender_id=t.id, mandatory=["Minimum £5m turnover"])
    r = client.get(f"/tenders/{t.id}/go-no-go")
    assert r.status_code == 200
    body = r.json()
    assert body["tender_id"] == t.id
    assert body["recommendation"] in {"go", "conditional", "no_go"}
    assert "red_warnings" in body
    assert "missing_info" in body
    assert len(body["self_certification"]) == 3
    assert "reconciliation" in body
    assert "working_days_remaining" in body


def test_go_no_go_404_for_unknown_tender(client) -> None:
    r = client.get("/tenders/99999/go-no-go")
    assert r.status_code == 404


def test_go_no_go_409_when_no_brief_yet(client, db) -> None:
    t = _make_tender(db)
    r = client.get(f"/tenders/{t.id}/go-no-go")
    assert r.status_code == 409
    body = r.json()
    assert body["detail"]["code"] == "brief_not_ready"


def test_vault_reconciliation_endpoint_returns_warnings(client, db) -> None:
    t = _make_tender(db)
    _make_brief(db, tender_id=t.id, mandatory=["Minimum £5m turnover"])
    # Vault doc that triggers a contradiction.
    doc = VaultDocument(org_id=1, category="accounts", title="Accounts 2025")
    db.add(doc)
    db.flush()
    v = VaultDocumentVersion(
        document_id=doc.id,
        version=1,
        storage_key="x",
        bytes=1,
        sha256="x",
        mime_type="application/pdf",
        title="Accounts 2025",
        claims={"doc_type": "accounts", "turnover": "3000000"},
    )
    db.add(v)
    db.flush()
    doc.current_version_id = v.id
    db.commit()

    r = client.get(f"/tenders/{t.id}/vault-reconciliation")
    assert r.status_code == 200
    body = r.json()
    assert any(w["kind"] == "contradiction" for w in body["warnings"])
