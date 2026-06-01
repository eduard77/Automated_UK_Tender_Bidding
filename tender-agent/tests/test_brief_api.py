"""API: brief generation task + latest brief read (chunk 5 Part C).

The schedule_brief background runner is monkeypatched to a no-op so the
TestClient never triggers a real LLM call. Per-test, we drive the brief row
straight to the desired status to exercise the API surface.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tender_agent.api import tender_brief as tb_mod
from tender_agent.db import engine, get_db
from tender_agent.main import app
from tender_agent.models import (
    Tender,
    TenderBrief,
    TenderDocumentFile,
)
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


@pytest.fixture()
def client(session: Session, monkeypatch) -> TestClient:
    """TestClient that is already authenticated as a plan_unlimited account.

    Chunk 6 gates the brief endpoints — anonymous callers receive a 50%
    teaser (and 401/402 on writes). These tests pre-date the gate and
    assert the entitled behaviour, so we lift the client past the gate
    once at fixture level. The dev override is the same path the dashboard
    will use for local testing before Stripe keys are wired up; it's
    refused in production (TENDER_AGENT_ENV=production).
    """
    # Never run real generation during endpoint tests.
    async def _noop(brief_id: int, tender_id: int) -> None:
        return None

    monkeypatch.setattr(tb_mod, "schedule_brief", _noop)

    def override() -> Session:
        yield session

    app.dependency_overrides[get_db] = override
    tc = TestClient(app)
    try:
        authenticate_unlimited(tc)
        yield tc
    finally:
        app.dependency_overrides.pop(get_db, None)


def _tender(session: Session, ref: str) -> Tender:
    now = datetime.now(UTC)
    t = Tender(
        source_code="TEST",
        source_ref=ref,
        title=f"T {ref}",
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(t)
    session.flush()
    return t


def _document(session: Session, tender_id: int, name: str, body: bytes) -> TenderDocumentFile:
    now = datetime.now(UTC)
    f = TenderDocumentFile(
        tender_id=tender_id,
        url=f"https://example.com/{name}",
        title=name,
        format=name.rsplit(".", 1)[-1],
        storage_key=f"/tmp/{name}",
        storage_backend="local",
        bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
        download_status="ok",
        downloaded_at=now,
        created_at=now,
    )
    session.add(f)
    session.flush()
    return f


def test_post_generate_brief_returns_202_and_creates_row(
    client: TestClient, session: Session
) -> None:
    t = _tender(session, "api-1")
    session.flush()
    resp = client.post(f"/tenders/{t.id}/generate-brief")
    assert resp.status_code == 202
    body = resp.json()
    assert body["tender_id"] == t.id
    assert body["status"] == "generating"
    rows = session.query(TenderBrief).filter_by(tender_id=t.id).all()
    assert len(rows) == 1


def test_post_generate_brief_404_for_missing_tender(client: TestClient) -> None:
    resp = client.post("/tenders/99999999/generate-brief")
    assert resp.status_code == 404


def test_get_brief_returns_null_when_none(
    client: TestClient, session: Session
) -> None:
    t = _tender(session, "api-2")
    session.flush()
    resp = client.get(f"/tenders/{t.id}/brief")
    assert resp.status_code == 200
    assert resp.json() is None


def test_get_brief_returns_latest_complete(
    client: TestClient, session: Session
) -> None:
    t = _tender(session, "api-3")
    session.flush()
    now = datetime.now(UTC)
    older = TenderBrief(
        tender_id=t.id,
        status="complete",
        recommendation="no_bid",
        confidence="high",
        headline="older",
        brief_json={"recommendation": "no_bid"},
        model="m",
        generated_at=now,
        created_at=now.replace(microsecond=0),
        updated_at=now.replace(microsecond=0),
    )
    newer = TenderBrief(
        tender_id=t.id,
        status="complete",
        recommendation="bid",
        confidence="medium",
        headline="newer",
        brief_json={"recommendation": "bid"},
        model="m",
        generated_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add_all([older, newer])
    session.flush()

    resp = client.get(f"/tenders/{t.id}/brief")
    assert resp.status_code == 200
    body = resp.json()
    assert body is not None
    assert body["headline"] == "newer"
    assert body["recommendation"] == "bid"


def test_get_briefs_history_listed_newest_first(
    client: TestClient, session: Session
) -> None:
    t = _tender(session, "api-4")
    session.flush()
    base = datetime.now(UTC)
    for i in range(3):
        session.add(
            TenderBrief(
                tender_id=t.id,
                status="complete",
                recommendation="bid",
                headline=f"v{i}",
                created_at=base.replace(microsecond=i * 1000),
                updated_at=base.replace(microsecond=i * 1000),
            )
        )
    session.flush()
    resp = client.get(f"/tenders/{t.id}/briefs")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 3
    headlines = [r["headline"] for r in rows]
    assert headlines == ["v2", "v1", "v0"]


def test_regenerate_adds_new_brief_row(client: TestClient, session: Session) -> None:
    t = _tender(session, "api-5")
    session.flush()
    r1 = client.post(f"/tenders/{t.id}/generate-brief")
    r2 = client.post(f"/tenders/{t.id}/generate-brief")
    assert r1.status_code == 202 and r2.status_code == 202
    assert r1.json()["id"] != r2.json()["id"]
    rows = session.query(TenderBrief).filter_by(tender_id=t.id).all()
    assert len(rows) == 2


def test_documents_endpoint_reports_content_stored_flag(
    client: TestClient, session: Session, tmp_path, monkeypatch
) -> None:
    """The /tenders/{id}/documents response carries content_stored=True for
    files whose sha256 has a current-version content row."""
    from tender_agent.models import TenderDocumentContent
    from tender_agent.services.brief.content_store import EXTRACTOR_VERSION

    t = _tender(session, "api-6")
    session.flush()
    body = b"ITT text"
    sha = hashlib.sha256(body).hexdigest()
    f = _document(session, t.id, "ITT.txt", body)
    # Insert a content row that should flip the flag.
    now = datetime.now(UTC)
    session.add(
        TenderDocumentContent(
            document_file_id=f.id,
            tender_id=t.id,
            sha256=sha,
            extracted_text="ITT text",
            char_count=8,
            extraction_status="ok",
            doc_type="txt",
            extractor_version=EXTRACTOR_VERSION,
            created_at=now,
            updated_at=now,
        )
    )
    session.flush()

    resp = client.get(f"/tenders/{t.id}/documents")
    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload) == 1
    assert payload[0]["content_stored"] is True
