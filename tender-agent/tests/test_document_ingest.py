"""Server-side ingest of operator-fetched documents — DB rows + blob put.

DB-backed via the savepoint fixture; the blob backend is a fake that records
puts (no Azure). Proves: happy-path file + content rows, dedup/idempotency by
sha256 (re-ingest writes no duplicate rows and skips the re-upload), distinct
urls per document, and server-side sha recompute.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.db import engine
from tender_agent.models import Tender, TenderDocumentContent, TenderDocumentFile
from tender_agent.services.portals.document_ingest import (
    IngestItem,
    ingest_documents,
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


class FakeStorage:
    name = "azure_blob"

    def __init__(self):
        self.puts: list[tuple[str, int]] = []

    def put(self, key, data, *, content_type=None):
        self.puts.append((key, len(data)))

    def url(self, key):
        return f"https://acct.blob.core.windows.net/tender-documents/{key}"


def _tender(db: Session, ref: str) -> Tender:
    now = datetime.now(UTC)
    t = Tender(
        source_code="TEST", source_ref=ref, title=f"Tender {ref}",
        first_seen_at=now, last_seen_at=now,
    )
    db.add(t)
    db.flush()
    return t


def _item(text="hello world", *, title="spec.pdf", fmt="pdf"):
    data = f"%PDF {text}".encode()
    return (
        IngestItem(
            title=title, format=fmt, content_type="application/pdf",
            client_sha256=hashlib.sha256(data).hexdigest(),
            extracted_text=text, char_count=len(text), extraction_status="ok",
            doc_type="pdf", extractor_version="v1",
        ),
        data,
    )


def _files(db, tender_id):
    return list(db.execute(
        select(TenderDocumentFile).where(TenderDocumentFile.tender_id == tender_id)
    ).scalars().all())


def _content(db, tender_id):
    return list(db.execute(
        select(TenderDocumentContent).where(TenderDocumentContent.tender_id == tender_id)
    ).scalars().all())


def test_ingest_writes_file_and_content_rows(db):
    t = _tender(db, "ing-1")
    storage = FakeStorage()
    item, data = _item("requirements text")
    sha = hashlib.sha256(data).hexdigest()

    res = ingest_documents(db, t.id, [(item, data)], storage=storage)

    assert res.inserted == 1 and res.deduped == 0
    assert res.content_written == 1 and res.content_reused == 0
    # Blob written at {tender_id}/{sha}.{ext}.
    assert storage.puts == [(f"{t.id}/{sha}.pdf", len(data))]

    files = _files(db, t.id)
    assert len(files) == 1
    f = files[0]
    assert f.sha256 == sha
    assert f.storage_backend == "azure_blob"
    assert f.storage_key == f"{t.id}/{sha}.pdf"
    assert f.download_status == "ok"
    assert f.url  # synthetic, non-null

    content = _content(db, t.id)
    assert len(content) == 1
    assert content[0].extracted_text == "requirements text"
    assert content[0].extractor_version == "v1"


def test_ingest_is_idempotent_dedups_by_sha(db):
    t = _tender(db, "ing-2")
    storage = FakeStorage()
    item, data = _item("same content")

    first = ingest_documents(db, t.id, [(item, data)], storage=storage)
    assert first.inserted == 1
    second = ingest_documents(db, t.id, [(item, data)], storage=storage)

    # Re-ingest: deduped, no new file/content rows.
    assert second.inserted == 0 and second.deduped == 1
    assert second.content_reused == 1
    assert len(_files(db, t.id)) == 1
    assert len(_content(db, t.id)) == 1
    # The already-stored blob is NOT re-uploaded (one put total).
    assert len(storage.puts) == 1


def test_ingest_distinct_docs_get_distinct_rows_and_urls(db):
    t = _tender(db, "ing-3")
    storage = FakeStorage()
    items = [_item("doc A", title="a.pdf"), _item("doc B", title="b.pdf")]

    res = ingest_documents(db, t.id, items, storage=storage)
    assert res.inserted == 2
    files = _files(db, t.id)
    assert len(files) == 2
    assert len({f.url for f in files}) == 2  # distinct non-null urls
    assert len({f.sha256 for f in files}) == 2
    assert len(storage.puts) == 2


def test_ingest_recomputes_sha_and_flags_client_mismatch(db):
    t = _tender(db, "ing-4")
    storage = FakeStorage()
    item, data = _item("trust the bytes")
    server_sha = hashlib.sha256(data).hexdigest()
    item.client_sha256 = "0" * 64  # wrong on purpose

    res = ingest_documents(db, t.id, [(item, data)], storage=storage)
    # Server hash wins; the row + blob key use the recomputed sha.
    assert _files(db, t.id)[0].sha256 == server_sha
    assert storage.puts[0][0] == f"{t.id}/{server_sha}.pdf"
    assert res.files[0].sha_mismatch is True


def test_ingest_cross_tender_content_reuse(db):
    # The SAME file on a different tender reuses the content row (sha, version).
    t1 = _tender(db, "ing-5a")
    t2 = _tender(db, "ing-5b")
    storage = FakeStorage()
    item, data = _item("shared ITT body")

    ingest_documents(db, t1.id, [(item, data)], storage=storage)
    item2, data2 = _item("shared ITT body")  # identical bytes → same sha
    res2 = ingest_documents(db, t2.id, [(item2, data2)], storage=storage)

    # New file row on t2, but the content row is reused (not written again).
    assert len(_files(db, t2.id)) == 1
    assert res2.content_reused == 1 and res2.content_written == 0
