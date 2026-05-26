"""Content store reuse behaviour (Phase 4 chunk 5).

The whole point of `tender_document_content` is that identical bytes are
extracted ONCE and reused forever — including across tenders. These tests
prove that:

- re-running ensure_content_extracted on a tender we already extracted
  doesn't re-read files or re-extract (reused_same_tender goes up);
- a SECOND tender that points at the same sha256 reuses the existing
  extraction rather than reading the file again (reused_cross_tender);
- a bumped extractor_version triggers a fresh extraction (audit/version
  bumps must invalidate the cache).
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from tender_agent.db import engine
from tender_agent.models import (
    Tender,
    TenderDocumentContent,
    TenderDocumentFile,
)
from tender_agent.services.brief import content_store as cs_module
from tender_agent.services.brief.content_store import ensure_content_extracted
from tender_agent.services.brief.document_extractor import EXTRACTOR_VERSION


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


def _make_tender(session: Session, ref: str) -> Tender:
    now = datetime.now(UTC)
    t = Tender(
        source_code="FTS",
        source_ref=ref,
        title=f"tender {ref}",
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(t)
    session.flush()
    return t


def _make_doc_file(
    session: Session,
    tender: Tender,
    *,
    url: str,
    storage_key: str,
    sha: str,
    title: str = "doc.txt",
    fmt: str = "txt",
) -> TenderDocumentFile:
    f = TenderDocumentFile(
        tender_id=tender.id,
        url=url,
        title=title,
        format=fmt,
        storage_key=storage_key,
        storage_backend="local",
        bytes=42,
        sha256=sha,
        download_status="ok",
        downloaded_at=datetime.now(UTC),
    )
    session.add(f)
    session.flush()
    return f


def _write_storage_file(monkeypatch, tmp_path, body: bytes, *, rel: str) -> str:
    """Lay down a file under a real DOCUMENT_STORAGE_DIR and patch the
    settings to point at it."""
    monkeypatch.setattr(
        "tender_agent.services.brief.content_store.settings.document_storage_dir",
        str(tmp_path),
    )
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return rel


def test_extract_then_reuse_same_tender_does_not_reread_file(
    session, monkeypatch, tmp_path
):
    body = b"hello tender world"
    sha = hashlib.sha256(body).hexdigest()
    rel = _write_storage_file(monkeypatch, tmp_path, body, rel="a/file.txt")

    t = _make_tender(session, "reuse-1")
    _make_doc_file(session, t, url="https://e/1", storage_key=rel, sha=sha)
    session.refresh(t)

    first = ensure_content_extracted(session, t)
    assert first.extracted == 1
    assert first.reused_same_tender == 0
    assert len(first.contents) == 1
    assert first.contents[0].extraction_status == "ok"
    assert first.contents[0].extracted_text == "hello tender world"

    # Now SCRAMBLE the file on disk. If the extractor were to re-run, the
    # second call would either error (different content) or produce
    # different text. Instead it must hit the cache and reuse the row.
    target = tmp_path / rel
    target.write_bytes(b"this should not be read")

    second = ensure_content_extracted(session, t)
    assert second.extracted == 0
    assert second.reused_same_tender == 1
    assert second.reused_cross_tender == 0
    assert second.contents[0].extracted_text == "hello tender world"


def test_same_sha_on_second_tender_is_reused_cross_tender(
    session, monkeypatch, tmp_path
):
    """The cornerstone guarantee: a second tender containing the same ITT
    file MUST NOT re-extract or re-read the file from disk — it references
    the canonical extraction stored under the first tender that fetched it.
    Per the (sha256, extractor_version) unique constraint, there is exactly
    one row per byte blob globally."""
    body = b"shared ITT body"
    sha = hashlib.sha256(body).hexdigest()
    rel = _write_storage_file(monkeypatch, tmp_path, body, rel="b/shared.txt")

    t1 = _make_tender(session, "shared-1")
    _make_doc_file(session, t1, url="https://e/shared", storage_key=rel, sha=sha)
    session.refresh(t1)
    first = ensure_content_extracted(session, t1)
    assert first.extracted == 1
    canonical_id = first.contents[0].id

    # Second tender references the same content (same sha256). The extractor
    # must NOT be called again — if it were, we'd see extracted=1.
    t2 = _make_tender(session, "shared-2")
    _make_doc_file(session, t2, url="https://e/shared-2", storage_key=rel, sha=sha)
    session.refresh(t2)

    # Belt-and-braces: also blow away the file so the only way to succeed is
    # to reuse the existing extraction. (Cross-tender reuse must work
    # even when the file isn't on disk for this tender.)
    (tmp_path / rel).unlink()

    second = ensure_content_extracted(session, t2)
    assert second.extracted == 0
    assert second.reused_cross_tender == 1
    assert second.reused_same_tender == 0
    # The row referenced for t2 IS the canonical row stored under t1 —
    # one extraction per byte blob, reused everywhere.
    assert second.contents[0].id == canonical_id
    assert second.contents[0].extracted_text == "shared ITT body"

    # And only ONE row exists in the table for this sha256 (globally unique).
    rows = (
        session.query(TenderDocumentContent)
        .filter(TenderDocumentContent.sha256 == sha)
        .all()
    )
    assert len(rows) == 1


def test_bumped_extractor_version_triggers_re_extraction(
    session, monkeypatch, tmp_path
):
    body = b"version-sensitive content"
    sha = hashlib.sha256(body).hexdigest()
    rel = _write_storage_file(monkeypatch, tmp_path, body, rel="v/file.txt")

    t = _make_tender(session, "ver-1")
    _make_doc_file(session, t, url="https://e/v", storage_key=rel, sha=sha)
    session.refresh(t)
    ensure_content_extracted(session, t)

    # Bump the extractor version. The next call must re-extract because the
    # old row has the OLD version — the unique constraint on
    # (sha256, extractor_version) makes the two rows coexist for audit.
    monkeypatch.setattr(cs_module, "EXTRACTOR_VERSION", EXTRACTOR_VERSION + "-bumped")
    # The brief_generator module also caches EXTRACTOR_VERSION via the
    # ensure_content_extracted call path — patching the source here is
    # enough because content_store imports the symbol from
    # document_extractor at call time.
    monkeypatch.setattr(
        "tender_agent.services.brief.document_extractor.EXTRACTOR_VERSION",
        EXTRACTOR_VERSION + "-bumped",
    )

    second = ensure_content_extracted(session, t)
    assert second.extracted == 1
    assert second.reused_same_tender == 0
    # Both rows are kept (different extractor versions): the unique
    # constraint is (sha256, extractor_version), so a version bump allows a
    # second row alongside the first — old extractions stay for audit.
    rows = (
        session.query(TenderDocumentContent)
        .filter(TenderDocumentContent.sha256 == sha)
        .all()
    )
    assert len(rows) == 2
    assert {r.extractor_version for r in rows} == {
        EXTRACTOR_VERSION,
        EXTRACTOR_VERSION + "-bumped",
    }


def test_missing_storage_key_records_error_row(session, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "tender_agent.services.brief.content_store.settings.document_storage_dir",
        str(tmp_path),
    )
    t = _make_tender(session, "no-key")
    f = TenderDocumentFile(
        tender_id=t.id,
        url="https://e/x",
        title="orphan.txt",
        format="txt",
        storage_key=None,
        sha256=hashlib.sha256(b"x").hexdigest(),
        download_status="ok",
        downloaded_at=datetime.now(UTC),
    )
    session.add(f)
    session.flush()
    session.refresh(t)

    res = ensure_content_extracted(session, t)
    assert res.failed == 1
    assert res.contents[0].extraction_status == "error"


def test_missing_file_on_disk_records_error_not_raises(
    session, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        "tender_agent.services.brief.content_store.settings.document_storage_dir",
        str(tmp_path),
    )
    t = _make_tender(session, "no-file")
    _make_doc_file(
        session, t, url="https://e/gone", storage_key="gone/file.txt",
        sha=hashlib.sha256(b"nope").hexdigest(),
    )
    session.refresh(t)

    res = ensure_content_extracted(session, t)
    # Extractor returns status="error" with detail "file missing on disk"
    # but the call itself does NOT raise — fetch must keep going.
    assert res.failed == 1
    assert res.contents[0].extraction_status == "error"
    assert "missing on disk" in (res.contents[0].extraction_detail or "")
