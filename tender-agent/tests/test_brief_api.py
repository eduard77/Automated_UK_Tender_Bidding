"""POST /tenders/{id}/generate-brief and GET /tenders/{id}/brief (chunk 5).

We monkeypatch the background runner so the TestClient never invokes a real
LLM. Each test drives the lifecycle by manipulating TenderBrief rows
directly, mirroring the fetch-task pattern used in test_fetch_api.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tender_agent.api import tender_brief as tb_mod
from tender_agent.db import engine, get_db
from tender_agent.main import app
from tender_agent.models import Tender, TenderBrief


@pytest.fixture()
def session() -> Session:
    connection = engine.connect()
    outer = connection.begin()
    sess = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield sess
    finally:
        sess.close()
        outer.rollback()
        connection.close()


@pytest.fixture()
def client(session: Session, monkeypatch) -> TestClient:
    async def _noop(brief_id: int, tender_id: int) -> None:
        return None

    monkeypatch.setattr(tb_mod, "schedule_generate_brief", _noop)

    def override() -> Session:
        yield session

    app.dependency_overrides[get_db] = override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _tender(session: Session, ref: str = "brief-api-1") -> Tender:
    now = datetime.now(UTC)
    t = Tender(
        source_code="FTS",
        source_ref=ref,
        title="t",
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(t)
    session.flush()
    return t


def _attach_brief(
    session: Session,
    tender: Tender,
    *,
    status: str,
    headline: str | None = None,
    recommendation: str | None = None,
    minutes_ago: int = 0,
    payload: dict | None = None,
) -> TenderBrief:
    from datetime import timedelta

    when = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    b = TenderBrief(
        tender_id=tender.id,
        status=status,
        headline=headline,
        recommendation=recommendation,
        confidence="medium",
        brief_json=payload,
        documents_considered=[],
        model="fake",
        generated_at=when if status == "complete" else None,
        created_at=when,
        updated_at=when,
    )
    session.add(b)
    session.flush()
    return b


def test_start_generate_brief_returns_202_and_creates_placeholder(client, session):
    t = _tender(session)
    session.flush()
    resp = client.post(f"/tenders/{t.id}/generate-brief")
    assert resp.status_code == 202
    body = resp.json()
    assert body["tender_id"] == t.id
    assert body["status"] == "generating"
    assert body["id"]
    # Row exists in the DB.
    placeholder = session.get(TenderBrief, body["id"])
    assert placeholder is not None
    assert placeholder.status == "generating"


def test_start_generate_brief_404_for_missing_tender(client):
    resp = client.post("/tenders/99999999/generate-brief")
    assert resp.status_code == 404


def test_get_brief_404_when_none_generated(client, session):
    t = _tender(session, "brief-api-none")
    session.flush()
    resp = client.get(f"/tenders/{t.id}/brief")
    assert resp.status_code == 404


def test_get_brief_returns_latest_by_created_at(client, session):
    t = _tender(session, "brief-api-latest")
    session.flush()
    _attach_brief(
        session, t, status="complete",
        headline="OLD", recommendation="no_bid", minutes_ago=10,
        payload={"recommendation": "no_bid", "confidence": "low",
                 "headline": "OLD", "rationale": "", "key_risks": [],
                 "deadline": None, "contract_value": None,
                 "mandatory_requirements": [], "scoring": None,
                 "scope_summary": None, "notable_conditions": [],
                 "missing_or_unclear": []},
    )
    _attach_brief(
        session, t, status="complete",
        headline="NEW", recommendation="bid", minutes_ago=0,
        payload={"recommendation": "bid", "confidence": "high",
                 "headline": "NEW", "rationale": "", "key_risks": [],
                 "deadline": None, "contract_value": None,
                 "mandatory_requirements": [], "scoring": None,
                 "scope_summary": None, "notable_conditions": [],
                 "missing_or_unclear": []},
    )
    session.flush()
    resp = client.get(f"/tenders/{t.id}/brief")
    assert resp.status_code == 200
    body = resp.json()
    assert body["headline"] == "NEW"
    assert body["recommendation"] == "bid"


def test_regenerate_creates_a_new_row_keeping_history(client, session):
    t = _tender(session, "brief-api-history")
    _attach_brief(
        session, t, status="complete", headline="prior",
        recommendation="bid", minutes_ago=5,
    )
    session.flush()
    before = session.query(TenderBrief).filter_by(tender_id=t.id).count()
    resp = client.post(f"/tenders/{t.id}/generate-brief")
    assert resp.status_code == 202
    after = session.query(TenderBrief).filter_by(tender_id=t.id).count()
    assert after == before + 1


def test_get_brief_404_when_tender_missing(client):
    resp = client.get("/tenders/99999999/brief")
    assert resp.status_code == 404
