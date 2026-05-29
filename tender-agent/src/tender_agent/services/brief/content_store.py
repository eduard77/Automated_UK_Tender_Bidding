"""Content store — write/reuse extracted document text keyed by sha256.

This is the chunk 5 foundation: extract ONCE per (sha256, extractor_version),
reuse forever. A re-fetch of the same ITT, or a different tender that links to
the same ITT, never re-extracts — the row is already there.

The bytes on disk become a cache for the underlying file; the database row
holds the *content* — that's what's reusable, queryable, and (later)
movable to a cloud DB for cross-tender comparison.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.models import Tender, TenderDocumentContent, TenderDocumentFile
from tender_agent.services.brief.document_extractor import (
    EXTRACTOR_VERSION,
    ExtractedDoc,
    extract_document,
    resolve_storage_path,
)

logger = structlog.get_logger(__name__)


@dataclass
class ContentExtractionSummary:
    """Per-tender result of ensure_content_extracted.

    extracted = newly inserted rows (extraction work done now).
    reused    = existing rows we matched on (sha256, extractor_version).
    failed    = files where extraction returned status=error.
    rows      = the full per-file mapping (one entry per TenderDocumentFile).
    """

    extracted: int = 0
    reused: int = 0
    failed: int = 0
    rows: list[TenderDocumentContent] = field(default_factory=list)


def _hash_file(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:  # noqa: BLE001
        return None


def _find_existing(
    db: Session, sha256: str, extractor_version: str
) -> TenderDocumentContent | None:
    return db.execute(
        select(TenderDocumentContent)
        .where(TenderDocumentContent.sha256 == sha256)
        .where(TenderDocumentContent.extractor_version == extractor_version)
        .limit(1)
    ).scalar_one_or_none()


def _row_from(
    *,
    document_file_id: int,
    tender_id: int,
    sha256: str,
    extracted: ExtractedDoc,
) -> TenderDocumentContent:
    now = datetime.now(UTC)
    return TenderDocumentContent(
        document_file_id=document_file_id,
        tender_id=tender_id,
        sha256=sha256,
        extracted_text=extracted.text,
        char_count=extracted.char_count if extracted.char_count else None,
        extraction_status=extracted.status,
        extraction_detail=extracted.detail,
        doc_type=extracted.doc_type,
        extractor_version=EXTRACTOR_VERSION,
        created_at=now,
        updated_at=now,
    )


def ensure_content_extracted(
    db: Session, tender: Tender
) -> ContentExtractionSummary:
    """For each TenderDocumentFile on the tender, ensure a tender_document_content
    row exists for (sha256, EXTRACTOR_VERSION).

    Reuse rule: if any other tender has already extracted the same content
    (same sha256, same extractor_version), we look up that row and DON'T
    re-extract. The row's `tender_id` keeps pointing at whichever tender first
    triggered the extraction; the per-tender association lives implicitly in
    TenderDocumentFile.sha256, which is how the brief generator queries
    content for a specific tender.

    Never raises on a per-file extraction error — that file is recorded with
    status='error' so the brief generator still has the rest of the pack.
    """
    summary = ContentExtractionSummary()
    files = list(tender.document_files or [])
    if not files:
        logger.info(
            "brief.content_extracted",
            tender_id=tender.id,
            extracted=0,
            reused=0,
            failed=0,
            files=0,
        )
        return summary

    for f in files:
        if not f.storage_key:
            continue

        # If we have a sha256 on the file already (the orchestrator computes
        # one during download), trust it. Otherwise rehash from disk.
        sha = f.sha256
        if not sha:
            sha = _hash_file(resolve_storage_path(f.storage_key))
            if sha:
                f.sha256 = sha
        if not sha:
            logger.info(
                "brief.content_skip_no_sha", tender_id=tender.id, file_id=f.id
            )
            continue

        existing = _find_existing(db, sha, EXTRACTOR_VERSION)
        if existing is not None:
            # Reuse: file content already extracted (here or for another
            # tender). NO disk read, NO extraction work.
            summary.reused += 1
            summary.rows.append(existing)
            continue

        # Extract for the first time.
        extracted = extract_document(f.storage_key, f.title, f.format)
        if extracted.status == "error":
            summary.failed += 1
        row = _row_from(
            document_file_id=f.id,
            tender_id=tender.id,
            sha256=sha,
            extracted=extracted,
        )
        db.add(row)
        try:
            db.flush()
        except Exception as exc:  # noqa: BLE001
            # Race: another transaction inserted the same (sha256, ext_version)
            # between our SELECT and our INSERT. Roll back this row, re-fetch
            # the winning row, and reuse it.
            db.rollback()
            existing = _find_existing(db, sha, EXTRACTOR_VERSION)
            if existing is not None:
                summary.reused += 1
                summary.rows.append(existing)
                continue
            logger.warning(
                "brief.content_flush_failed",
                tender_id=tender.id,
                file_id=f.id,
                error=str(exc),
            )
            summary.failed += 1
            continue
        summary.extracted += 1
        summary.rows.append(row)

    db.commit()
    logger.info(
        "brief.content_extracted",
        tender_id=tender.id,
        extracted=summary.extracted,
        reused=summary.reused,
        failed=summary.failed,
        files=len(files),
    )
    return summary


def get_content_for_tender(
    db: Session, tender_id: int
) -> list[tuple[TenderDocumentFile, TenderDocumentContent]]:
    """Pair every TenderDocumentFile for a tender with its current-version
    content row (if any). Used by the brief generator."""
    files = list(
        db.execute(
            select(TenderDocumentFile).where(TenderDocumentFile.tender_id == tender_id)
        ).scalars().all()
    )
    pairs: list[tuple[TenderDocumentFile, TenderDocumentContent]] = []
    for f in files:
        if not f.sha256:
            continue
        row = _find_existing(db, f.sha256, EXTRACTOR_VERSION)
        if row is not None:
            pairs.append((f, row))
    return pairs
