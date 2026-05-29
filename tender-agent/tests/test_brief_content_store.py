"""Content store — REUSE by sha256 across re-fetches and across tenders.

The core guarantee of chunk 5 Part A: extract once, reuse forever. These
tests prove that:
  - re-running ensure_content_extracted on the same tender reuses rows (no
    second extraction, no second file read);
  - the SAME sha256 on a DIFFERENT tender also reuses (cross-tender reuse);
  - bumping EXTRACTOR_VERSION forces re-extraction.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from tender_agent.db import engine
from tender_agent.models import (
    Tender,
    TenderDocumentContent,
    TenderDocumentFile,
)
from tender_agent.services.brief import content_store as cs_mod
from tender_agent.services.brief.content_store import (
    EXTRACTOR_VERSION,
    ensure_content_extracted,
    get_content_for_tender,
)


@pytest.fixture()
def db() -> Session:
    connection = engine.connect()
    outer = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        connection.close()


def _write_text(tmp_path, name: str, content: bytes) -> str:
    p = tmp_path / name
    p.write_bytes(content)
    return str(p)


def _seed_tender(db: Session, ref: str) -> Tender:
    now = datetime.now(UTC)
    t = Tender(
        source_code="TEST",
        source_ref=ref,
        title=f"Tender {ref}",
        first_seen_at=now,
        last_seen_at=now,
    )
    db.add(t)
    db.flush()
    return t


def _seed_file(
    db: Session,
    tender_id: int,
    *,
    storage_key: str,
    filename: str,
    sha256: str,
    fmt: str = "txt",
) -> TenderDocumentFile:
    now = datetime.now(UTC)
    f = TenderDocumentFile(
        tender_id=tender_id,
        url=f"https://example.com/{filename}",
        title=filename,
        format=fmt,
        storage_key=storage_key,
        storage_backend="local",
        bytes=1,
        sha256=sha256,
        download_status="ok",
        downloaded_at=now,
        created_at=now,
    )
    db.add(f)
    db.flush()
    return f


def test_first_call_extracts_and_inserts(db: Session, tmp_path) -> None:
    body = b"ITT Specification content."
    path = _write_text(tmp_path, "spec.txt", body)
    sha = hashlib.sha256(body).hexdigest()
    t = _seed_tender(db, "cs-1")
    _seed_file(db, t.id, storage_key=path, filename="spec.txt", sha256=sha)

    summary = ensure_content_extracted(db, t)

    assert summary.extracted == 1
    assert summary.reused == 0
    assert summary.failed == 0
    rows = (
        db.query(TenderDocumentContent)
        .filter_by(sha256=sha, extractor_version=EXTRACTOR_VERSION)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].extraction_status == "ok"
    assert rows[0].extracted_text and "ITT Specification" in rows[0].extracted_text


def test_second_call_same_tender_reuses_no_file_reread(db: Session, tmp_path) -> None:
    body = b"Quality questionnaire content"
    path = _write_text(tmp_path, "qq.txt", body)
    sha = hashlib.sha256(body).hexdigest()
    t = _seed_tender(db, "cs-2")
    _seed_file(db, t.id, storage_key=path, filename="qq.txt", sha256=sha)

    first = ensure_content_extracted(db, t)
    assert first.extracted == 1

    # Re-running must not extract again. Stub extract_document to fail if
    # called — proves no second extraction happens.
    with patch.object(cs_mod, "extract_document") as fake:
        fake.side_effect = AssertionError("extract_document MUST NOT be called on reuse")
        # Refresh the tender so we don't have stale relations
        db.expire_all()
        t2 = db.get(Tender, t.id)
        second = ensure_content_extracted(db, t2)

    assert second.extracted == 0
    assert second.reused == 1
    rows = (
        db.query(TenderDocumentContent)
        .filter_by(sha256=sha, extractor_version=EXTRACTOR_VERSION)
        .all()
    )
    assert len(rows) == 1  # still one row


def test_same_sha_on_second_tender_reuses_across_tenders(db: Session, tmp_path) -> None:
    body = b"Shared ITT used by two tenders"
    path1 = _write_text(tmp_path, "itt-a.txt", body)
    path2 = _write_text(tmp_path, "itt-b.txt", body)  # different file, same content
    sha = hashlib.sha256(body).hexdigest()
    t1 = _seed_tender(db, "cs-3a")
    t2 = _seed_tender(db, "cs-3b")
    _seed_file(db, t1.id, storage_key=path1, filename="itt-a.txt", sha256=sha)
    _seed_file(db, t2.id, storage_key=path2, filename="itt-b.txt", sha256=sha)

    s1 = ensure_content_extracted(db, t1)
    assert s1.extracted == 1

    with patch.object(cs_mod, "extract_document") as fake:
        fake.side_effect = AssertionError(
            "cross-tender reuse must NOT re-extract identical content"
        )
        db.expire_all()
        t2 = db.get(Tender, t2.id)
        s2 = ensure_content_extracted(db, t2)

    assert s2.extracted == 0
    assert s2.reused == 1
    # Still ONE row globally for this (sha256, extractor_version).
    rows = (
        db.query(TenderDocumentContent)
        .filter_by(sha256=sha, extractor_version=EXTRACTOR_VERSION)
        .all()
    )
    assert len(rows) == 1


def test_extractor_version_bump_forces_re_extraction(db: Session, tmp_path) -> None:
    body = b"Spec content v1"
    path = _write_text(tmp_path, "spec.txt", body)
    sha = hashlib.sha256(body).hexdigest()
    t = _seed_tender(db, "cs-4")
    _seed_file(db, t.id, storage_key=path, filename="spec.txt", sha256=sha)

    first = ensure_content_extracted(db, t)
    assert first.extracted == 1

    # Simulate an extractor_version bump.
    with patch.object(cs_mod, "EXTRACTOR_VERSION", "v2-test"):
        db.expire_all()
        t = db.get(Tender, t.id)
        second = ensure_content_extracted(db, t)

    assert second.extracted == 1, "version bump must trigger re-extraction"
    versions = sorted(
        v
        for (v,) in db.query(TenderDocumentContent.extractor_version)
        .filter_by(sha256=sha)
        .all()
    )
    assert EXTRACTOR_VERSION in versions
    assert "v2-test" in versions


def test_failed_extraction_records_error_status(db: Session, tmp_path) -> None:
    # Storage key points at a non-existent file → extractor returns 'error'.
    sha = hashlib.sha256(b"missing").hexdigest()
    t = _seed_tender(db, "cs-5")
    _seed_file(
        db, t.id, storage_key=str(tmp_path / "ghost.txt"), filename="ghost.txt", sha256=sha
    )

    summary = ensure_content_extracted(db, t)

    assert summary.failed == 1
    row = (
        db.query(TenderDocumentContent)
        .filter_by(sha256=sha, extractor_version=EXTRACTOR_VERSION)
        .one()
    )
    assert row.extraction_status == "error"
    assert row.extraction_detail


def test_tender_without_files_returns_empty_summary(db: Session) -> None:
    t = _seed_tender(db, "cs-6")
    summary = ensure_content_extracted(db, t)
    assert summary.extracted == 0
    assert summary.reused == 0
    assert summary.failed == 0


def test_get_content_for_tender_joins_through_sha(db: Session, tmp_path) -> None:
    body = b"ITT content for tender lookup."
    path = _write_text(tmp_path, "itt.txt", body)
    sha = hashlib.sha256(body).hexdigest()
    t = _seed_tender(db, "cs-7")
    _seed_file(db, t.id, storage_key=path, filename="itt.txt", sha256=sha)

    ensure_content_extracted(db, t)
    pairs = get_content_for_tender(db, t.id)

    assert len(pairs) == 1
    f, c = pairs[0]
    assert f.sha256 == sha
    assert c.sha256 == sha
    assert c.extraction_status == "ok"
