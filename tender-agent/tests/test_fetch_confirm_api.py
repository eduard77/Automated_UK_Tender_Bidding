"""Confirm / cancel endpoints for the Express-Interest pause.

schedule_fetch_resume is monkeypatched to a no-op so the TestClient never runs
real orchestration.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tender_agent.api import tender_fetch as tf_mod
from tender_agent.db import engine, get_db
from tender_agent.main import app
from tender_agent.models import FetchTask, Tender


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
def client(session: Session, monkeypatch) -> TestClient:
    async def _noop(task_id: str, tender_id: int) -> None:
        return None

    monkeypatch.setattr(tf_mod, "schedule_fetch_resume", _noop)

    def override() -> Session:
        yield session

    app.dependency_overrides[get_db] = override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _tender(session: Session, ref="confirm-1") -> Tender:
    now = datetime.now(UTC)
    t = Tender(source_code="FTS", source_ref=ref, title="t", first_seen_at=now, last_seen_at=now)
    session.add(t)
    session.flush()
    return t


def _task(session: Session, tender_id: int, status: str) -> FetchTask:
    now = datetime.now(UTC)
    task = FetchTask(
        task_id=f"task-{tender_id}-{status}",
        tender_id=tender_id,
        status=status,
        created_at=now,
        updated_at=now,
    )
    session.add(task)
    session.flush()
    return task


def test_confirm_resumes_when_awaiting(client, session):
    t = _tender(session)
    task = _task(session, t.id, "needs_user_confirmation")
    session.flush()
    resp = client.post(f"/tenders/{t.id}/fetch-documents/{task.task_id}/confirm")
    assert resp.status_code == 202
    assert resp.json()["status"] == "queued"


def test_confirm_409_when_not_awaiting(client, session):
    t = _tender(session, "confirm-2")
    task = _task(session, t.id, "running")
    session.flush()
    resp = client.post(f"/tenders/{t.id}/fetch-documents/{task.task_id}/confirm")
    assert resp.status_code == 409


def test_confirm_404_unknown_task(client, session):
    t = _tender(session, "confirm-3")
    session.flush()
    resp = client.post(f"/tenders/{t.id}/fetch-documents/nope/confirm")
    assert resp.status_code == 404


def test_cancel_marks_failed(client, session):
    t = _tender(session, "cancel-1")
    task = _task(session, t.id, "waiting_for_login")
    session.flush()
    resp = client.post(f"/tenders/{t.id}/fetch-documents/{task.task_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "failed"
    assert "cancelled" in resp.json()["detail"].lower()
