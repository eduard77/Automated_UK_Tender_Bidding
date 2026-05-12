"""Tests for POST /admin/extract-requirements/{tender_id}.

We cover the two error paths that don't need Anthropic:
- 404 when the tender doesn't exist.
- 422 when the tender exists but has no description AND no downloaded documents.

The happy path requires a live Anthropic call and is covered by the
end-to-end validation script (`scripts/validate_extractor.py`); mocking it
exhaustively here would duplicate the unit tests in `test_requirements_extractor.py`.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from tender_agent.config import settings
from tender_agent.db import get_db
from tender_agent.main import app
from tender_agent.models import Tender


def _fake_db_with(get_result: object | None) -> MagicMock:
    """A MagicMock that quacks like a SQLAlchemy Session for our needs."""
    db = MagicMock()
    db.get = MagicMock(return_value=get_result)
    return db


def _client(db: MagicMock) -> TestClient:
    """Build a TestClient with get_db overridden to yield our fake."""

    def override() -> object:
        yield db

    app.dependency_overrides[get_db] = override
    return TestClient(app)


def _cleanup() -> None:
    app.dependency_overrides.pop(get_db, None)


def _tender(*, description: str | None, has_documents: bool) -> Tender:
    t = Tender(
        source_code="FTS",
        source_ref="ref-extract-test",
        title="Smoke",
        description=description,
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )
    t.id = 42
    t.document_files = ["fake-doc"] if has_documents else []
    return t


def test_extract_404_when_tender_missing(monkeypatch) -> None:
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    db = _fake_db_with(None)
    client = _client(db)
    try:
        res = client.post("/admin/extract-requirements/999")
    finally:
        _cleanup()
    assert res.status_code == 404
    assert res.json()["detail"] == "tender not found"


def test_extract_422_when_nothing_to_extract(monkeypatch) -> None:
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    tender = _tender(description=None, has_documents=False)
    db = _fake_db_with(tender)
    client = _client(db)
    try:
        res = client.post(f"/admin/extract-requirements/{tender.id}")
    finally:
        _cleanup()
    assert res.status_code == 422
    assert "nothing to extract" in res.json()["detail"]


def test_extract_422_when_description_is_whitespace_only(monkeypatch) -> None:
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    tender = _tender(description="   \n  ", has_documents=False)
    db = _fake_db_with(tender)
    client = _client(db)
    try:
        res = client.post(f"/admin/extract-requirements/{tender.id}")
    finally:
        _cleanup()
    assert res.status_code == 422


def test_extract_503_when_api_key_missing(monkeypatch) -> None:
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    tender = _tender(description="A real description.", has_documents=False)
    db = _fake_db_with(tender)
    client = _client(db)
    try:
        res = client.post(f"/admin/extract-requirements/{tender.id}")
    finally:
        _cleanup()
    assert res.status_code == 503
    assert "anthropic_api_key" in res.json()["detail"]
