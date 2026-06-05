"""Server-side ingest of operator-fetched documents (local-fetch pivot).

The operator's machine fetches Delta documents locally (real browser, residential
IP — the cloud IP is 403'd), extracts text, and POSTs each document's metadata +
extracted text + raw bytes to `/tenders/{id}/ingest-documents`. This module is the
write path that endpoint calls.

Why a PARALLEL persistence path (not `_persist_documents`): the orchestrator's
`_persist_documents` reads files off LOCAL disk and hard-codes
`storage_backend="local"`. Here the bytes arrive over HTTP and must land in Azure
Blob, and the extracted text is supplied by the operator (the file isn't on the
cloud's disk to re-extract). The DB shape and dedup rules are identical, though:
`tender_document_files` deduped by (tender_id, sha256), `tender_document_content`
keyed by (sha256, extractor_version) — reusing the same models and semantics.

Idempotent: re-ingesting the same tender's same documents writes no duplicate rows
and (for an already-stored file) skips the blob upload.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.models import TenderDocumentContent, TenderDocumentFile
from tender_agent.services.brief.document_extractor import EXTRACTOR_VERSION
from tender_agent.services.storage import StorageBackend

logger = structlog.get_logger(__name__)


@dataclass
class IngestItem:
    """Per-document metadata the operator sends alongside the file bytes. Only
    `title`/`format` and the extracted-text fields are trusted as-is; the sha256
    and byte length are RECOMPUTED server-side from the bytes (the client value,
    if any, is just a cross-check)."""

    title: str | None = None
    format: str | None = None
    content_type: str | None = None
    client_sha256: str | None = None
    extracted_text: str | None = None
    char_count: int | None = None
    extraction_status: str | None = None
    extraction_detail: str | None = None
    doc_type: str | None = None
    extractor_version: str | None = None


@dataclass
class IngestFileOutcome:
    sha256: str
    storage_key: str
    outcome: str  # "inserted" | "updated" | "deduped"
    content: str  # "written" | "reused" | "skipped"
    bytes: int
    title: str | None = None
    sha_mismatch: bool = False


@dataclass
class IngestResult:
    tender_id: int
    inserted: int = 0
    updated: int = 0
    deduped: int = 0
    content_written: int = 0
    content_reused: int = 0
    files: list[IngestFileOutcome] = field(default_factory=list)


def _ext_for(item: IngestItem) -> str:
    """A short file extension for the blob key: the format hint, else the title's
    extension, else empty."""
    fmt = (item.format or "").lower().lstrip(".")
    if fmt.isalnum() and fmt:
        return fmt
    title = item.title or ""
    if "." in title:
        cand = title.rsplit(".", 1)[-1].lower()
        if cand.isalnum():
            return cand
    return ""


def _format_token(item: IngestItem) -> str | None:
    """Value for TenderDocumentFile.format (varchar(32)) — the extension token,
    capped to fit the column."""
    ext = _ext_for(item)
    return ext[:32] if ext else None


def _synthetic_url(tender_id: int, sha: str) -> str:
    """A stable, distinct, non-null url for the (tender_id, url) unique
    constraint. Delta exposes no reliable per-document URL, so we synthesize one
    from (tender_id, sha256). Stable → re-ingest matches the same row; distinct →
    two documents never collide onto one row."""
    return f"delta-ingest://{tender_id}/{sha}"


def _ensure_content(
    db: Session,
    *,
    document_file_id: int,
    tender_id: int,
    sha: str,
    item: IngestItem,
) -> bool:
    """Ensure a tender_document_content row exists for (sha, extractor_version),
    storing the OPERATOR-supplied extracted text. Returns True if a new row was
    written, False if an existing row was reused. Mirrors content_store's reuse +
    flush-race handling."""
    version = item.extractor_version or EXTRACTOR_VERSION

    def _existing() -> TenderDocumentContent | None:
        return db.execute(
            select(TenderDocumentContent)
            .where(TenderDocumentContent.sha256 == sha)
            .where(TenderDocumentContent.extractor_version == version)
            .limit(1)
        ).scalar_one_or_none()

    if _existing() is not None:
        return False

    now = datetime.now(UTC)
    text = item.extracted_text
    row = TenderDocumentContent(
        document_file_id=document_file_id,
        tender_id=tender_id,
        sha256=sha,
        extracted_text=text,
        char_count=(item.char_count if item.char_count else (len(text) if text else None)),
        extraction_status=item.extraction_status or ("ok" if text else "empty"),
        extraction_detail=item.extraction_detail,
        doc_type=item.doc_type or _format_token(item),
        extractor_version=version,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    try:
        db.flush()
    except Exception as exc:  # noqa: BLE001
        # Race: another writer inserted the same (sha, version) between our
        # SELECT and INSERT. Roll back this row and reuse the winner.
        db.rollback()
        if _existing() is not None:
            return False
        logger.warning(
            "ingest.content_flush_failed", tender_id=tender_id, sha256=sha,
            error=str(exc),
        )
        raise
    return True


def ingest_documents(
    db: Session,
    tender_id: int,
    items: list[tuple[IngestItem, bytes]],
    *,
    storage: StorageBackend,
) -> IngestResult:
    """Persist operator-fetched documents: write each file's bytes to `storage`
    (Azure Blob in cloud), upsert a TenderDocumentFile row (dedup by
    (tender_id, sha256)), and store the operator's extracted text in
    tender_document_content (dedup by (sha256, extractor_version)).

    Idempotent: re-ingesting the same documents inserts no duplicate rows, and a
    file already stored under the same backend+key is not re-uploaded. The sha256
    is recomputed server-side from the bytes (never trusted from the client)."""
    result = IngestResult(tender_id=tender_id)
    for item, data in items:
        sha = hashlib.sha256(data).hexdigest()
        mismatch = bool(item.client_sha256) and item.client_sha256 != sha
        if mismatch:
            logger.info(
                "ingest.sha_mismatch", tender_id=tender_id,
                client=item.client_sha256, server=sha,
            )
        ext = _ext_for(item)
        key = f"{tender_id}/{sha}{('.' + ext) if ext else ''}"

        existing = db.execute(
            select(TenderDocumentFile)
            .where(TenderDocumentFile.tender_id == tender_id)
            .where(TenderDocumentFile.sha256 == sha)
            .limit(1)
        ).scalar_one_or_none()

        already_stored = (
            existing is not None
            and existing.storage_backend == storage.name
            and existing.storage_key == key
        )
        if not already_stored:
            storage.put(key, data, content_type=item.content_type)

        if existing is not None:
            # Dedup/idempotent: refresh metadata in place, never a new row.
            existing.title = item.title or existing.title
            existing.format = _format_token(item) or existing.format
            existing.storage_key = key
            existing.storage_backend = storage.name
            existing.bytes = len(data)
            existing.download_status = "ok"
            existing.error = None
            existing.downloaded_at = datetime.now(UTC)
            rec = existing
            outcome = "deduped"
            result.deduped += 1
        else:
            rec = TenderDocumentFile(
                tender_id=tender_id,
                url=_synthetic_url(tender_id, sha),
                title=item.title,
                format=_format_token(item),
                storage_key=key,
                storage_backend=storage.name,
                bytes=len(data),
                sha256=sha,
                download_status="ok",
                downloaded_at=datetime.now(UTC),
            )
            db.add(rec)
            outcome = "inserted"
            result.inserted += 1
        db.flush()

        content_written = _ensure_content(
            db, document_file_id=rec.id, tender_id=tender_id, sha=sha, item=item
        )
        if content_written:
            result.content_written += 1
        else:
            result.content_reused += 1

        result.files.append(
            IngestFileOutcome(
                sha256=sha,
                storage_key=key,
                outcome=outcome,
                content="written" if content_written else "reused",
                bytes=len(data),
                title=item.title,
                sha_mismatch=mismatch,
            )
        )

    db.commit()
    logger.info(
        "ingest.documents", tender_id=tender_id, items=len(items),
        inserted=result.inserted, updated=result.updated, deduped=result.deduped,
        content_written=result.content_written, content_reused=result.content_reused,
        storage=storage.name,
    )
    return result


def make_ingest_items(
    metadata: list[dict[str, Any]], files_by_index: dict[int, bytes]
) -> list[tuple[IngestItem, bytes]]:
    """Pair per-document metadata dicts with their uploaded bytes by list index.
    Metadata entries with no corresponding file part are dropped (logged)."""
    items: list[tuple[IngestItem, bytes]] = []
    for i, md in enumerate(metadata):
        data = files_by_index.get(i)
        if data is None:
            logger.info("ingest.missing_file_part", index=i)
            continue
        items.append((
            IngestItem(
                title=md.get("title"),
                format=md.get("format"),
                content_type=md.get("content_type"),
                client_sha256=md.get("sha256"),
                extracted_text=md.get("extracted_text"),
                char_count=md.get("char_count"),
                extraction_status=md.get("extraction_status"),
                extraction_detail=md.get("extraction_detail"),
                doc_type=md.get("doc_type"),
                extractor_version=md.get("extractor_version"),
            ),
            data,
        ))
    return items
