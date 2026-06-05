"""POST /tenders/{id}/ingest-documents — endpoint behaviour.

DB-backed (savepoint) + TestClient. The blob backend is monkeypatched to a fake
recorder so no Azure is touched. Covers: 401 anonymous, happy-path multipart
write, dedup/idempotency on re-POST, 404 unknown tender, 413 oversize, 400 bad
metadata.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.config import settings
from tender_agent.db import engine, get_db
from tender_agent.main import app
from tender_agent.models import Tender, TenderDocumentContent, TenderDocumentFile
from tests._auth_helpers import authenticate_unlimited


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


class _FakeStorage:
    name = "azure_blob"

    def __init__(self):
        self.puts = []

    def put(self, key, data, *, content_type=None):
        self.puts.append((key, len(data)))

    def url(self, key):
        return f"https://acct.blob.core.windows.net/tender-documents/{key}"


@pytest.fixture()
def fake_storage(monkeypatch) -> _FakeStorage:
    fake = _FakeStorage()
    monkeypatch.setattr(
        "tender_agent.api.document_ingest.get_storage_backend", lambda: fake
    )
    return fake


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


def _seed_tender(session: Session, ref: str) -> Tender:
    now = datetime.now(UTC)
    t = Tender(
        source_code="TEST", source_ref=ref, title=f"Tender {ref}",
        first_seen_at=now, last_seen_at=now,
    )
    session.add(t)
    session.flush()
    return t


def _doc(text="spec body", title="spec.pdf"):
    data = f"%PDF {text}".encode()
    md = {
        "title": title, "format": "pdf", "content_type": "application/pdf",
        "extracted_text": text, "char_count": len(text),
        "extraction_status": "ok", "doc_type": "pdf", "extractor_version": "v1",
    }
    return md, (title, data, "application/pdf")


def test_ingest_rejects_anonymous(anon_client, session):
    t = _seed_tender(session, "api-anon")
    md, filepart = _doc()
    r = anon_client.post(
        f"/tenders/{t.id}/ingest-documents",
        data={"metadata": json.dumps([md])},
        files=[("files", filepart)],
    )
    assert r.status_code == 401


def test_ingest_happy_path_writes_rows(auth_client, session, fake_storage):
    t = _seed_tender(session, "api-1")
    md, filepart = _doc("requirements here")
    r = auth_client.post(
        f"/tenders/{t.id}/ingest-documents",
        data={"metadata": json.dumps([md])},
        files=[("files", filepart)],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["inserted"] == 1
    assert body["content_written"] == 1
    assert len(fake_storage.puts) == 1

    files = session.execute(
        select(TenderDocumentFile).where(TenderDocumentFile.tender_id == t.id)
    ).scalars().all()
    assert len(files) == 1 and files[0].storage_backend == "azure_blob"
    content = session.execute(
        select(TenderDocumentContent).where(TenderDocumentContent.tender_id == t.id)
    ).scalars().all()
    assert len(content) == 1 and content[0].extracted_text == "requirements here"


def test_ingest_is_idempotent_on_repost(auth_client, session, fake_storage):
    t = _seed_tender(session, "api-2")
    md, filepart = _doc("same again")
    payload = {
        "data": {"metadata": json.dumps([md])},
        "files": [("files", filepart)],
    }
    r1 = auth_client.post(f"/tenders/{t.id}/ingest-documents", **payload)
    assert r1.status_code == 200 and r1.json()["inserted"] == 1
    r2 = auth_client.post(f"/tenders/{t.id}/ingest-documents", **payload)
    assert r2.status_code == 200
    assert r2.json()["deduped"] == 1 and r2.json()["inserted"] == 0

    files = session.execute(
        select(TenderDocumentFile).where(TenderDocumentFile.tender_id == t.id)
    ).scalars().all()
    assert len(files) == 1  # no duplicate
    # The already-stored blob is not re-uploaded.
    assert len(fake_storage.puts) == 1


def test_ingest_unknown_tender_404(auth_client, fake_storage):
    md, filepart = _doc()
    r = auth_client.post(
        "/tenders/999999/ingest-documents",
        data={"metadata": json.dumps([md])},
        files=[("files", filepart)],
    )
    assert r.status_code == 404


def test_ingest_oversize_413(auth_client, session, fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "ingest_max_bytes", 4)
    t = _seed_tender(session, "api-3")
    md, filepart = _doc("a much longer body than four bytes")
    r = auth_client.post(
        f"/tenders/{t.id}/ingest-documents",
        data={"metadata": json.dumps([md])},
        files=[("files", filepart)],
    )
    assert r.status_code == 413


def test_ingest_bad_metadata_400(auth_client, session, fake_storage):
    t = _seed_tender(session, "api-4")
    _, filepart = _doc()
    r = auth_client.post(
        f"/tenders/{t.id}/ingest-documents",
        data={"metadata": "{not json"},
        files=[("files", filepart)],
    )
    assert r.status_code == 400
