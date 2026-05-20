"""API: fetch-documents task lifecycle + document file serving.

The background runner (schedule_fetch) is monkeypatched to a no-op so the
TestClient never triggers real orchestration / network.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tender_agent.api import tender_fetch as tf_mod
from tender_agent.db import engine, get_db
from tender_agent.main import app
from tender_agent.models import Tender, TenderDocumentFile


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
    # Never run real orchestration during endpoint tests.
    async def _noop(task_id: str, tender_id: int) -> None:
        return None

    monkeypatch.setattr(tf_mod, "schedule_fetch", _noop)

    def override() -> Session:
        yield session

    app.dependency_overrides[get_db] = override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _tender(session: Session, ref: str = "fetch-api-1") -> Tender:
    now = datetime.now(UTC)
    t = Tender(
        source_code="CF",
        source_ref=ref,
        title="t",
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(t)
    session.flush()
    return t


def test_start_fetch_creates_task(client: TestClient, session: Session) -> None:
    t = _tender(session)
    session.flush()
    resp = client.post(f"/tenders/{t.id}/fetch-documents")
    assert resp.status_code == 202
    body = resp.json()
    assert body["tender_id"] == t.id
    assert body["status"] == "queued"
    assert body["task_id"]

    status = client.get(f"/tenders/{t.id}/fetch-documents/{body['task_id']}")
    assert status.status_code == 200
    assert status.json()["task_id"] == body["task_id"]


def test_start_fetch_404_for_missing_tender(client: TestClient) -> None:
    resp = client.post("/tenders/99999999/fetch-documents")
    assert resp.status_code == 404


def test_fetch_status_404_for_unknown_task(client: TestClient, session: Session) -> None:
    t = _tender(session, ref="fetch-api-2")
    session.flush()
    resp = client.get(f"/tenders/{t.id}/fetch-documents/deadbeef")
    assert resp.status_code == 404


def test_serve_document_file(
    client: TestClient, session: Session, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "tender_agent.api.tender_fetch.settings.document_storage_dir", str(tmp_path)
    )
    t = _tender(session, ref="serve-1")
    session.flush()
    rel = "serve/aa/aaaa.pdf"
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"%PDF-1.4 served")
    doc = TenderDocumentFile(
        tender_id=t.id,
        url="https://assets.publishing.service.gov.uk/x.pdf",
        title="ITT.pdf",
        format="pdf",
        storage_key=rel,
        storage_backend="local",
        bytes=15,
        sha256="aaaa",
        download_status="ok",
        downloaded_at=datetime.now(UTC),
    )
    session.add(doc)
    session.flush()

    resp = client.get(f"/tenders/{t.id}/documents/{doc.id}/file")
    assert resp.status_code == 200
    assert resp.content == b"%PDF-1.4 served"
    assert "itt.pdf" in resp.headers.get("content-disposition", "").lower()


def test_serve_404_wrong_tender(
    client: TestClient, session: Session, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "tender_agent.api.tender_fetch.settings.document_storage_dir", str(tmp_path)
    )
    t = _tender(session, ref="serve-2")
    session.flush()
    doc = TenderDocumentFile(
        tender_id=t.id,
        url="u",
        storage_key="x/y/z.pdf",
        download_status="ok",
        downloaded_at=datetime.now(UTC),
    )
    session.add(doc)
    session.flush()
    # Wrong tender id in the path.
    resp = client.get(f"/tenders/{t.id + 12345}/documents/{doc.id}/file")
    assert resp.status_code == 404


def test_serve_409_not_downloaded(
    client: TestClient, session: Session
) -> None:
    t = _tender(session, ref="serve-3")
    session.flush()
    doc = TenderDocumentFile(
        tender_id=t.id,
        url="u",
        storage_key=None,
        download_status="pending",
    )
    session.add(doc)
    session.flush()
    resp = client.get(f"/tenders/{t.id}/documents/{doc.id}/file")
    assert resp.status_code == 409
