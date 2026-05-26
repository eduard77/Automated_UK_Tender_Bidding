"""Content store: extract document text once, reuse forever.

The design intent (Phase 4 chunk 5) is that document CONTENT becomes durable,
reusable database data — not the file on one machine's disk. So:

- We key extractions by (sha256, extractor_version). Same bytes + same
  extractor = ONE canonical extraction. Re-running fetch never re-extracts.
- A second tender that points at the *same* document (same sha256) copies
  the existing extracted_text into a new row for that tender — content is
  reused across tenders, not re-read from disk and not re-extracted.
- The on-disk file IS NOT deleted by this module. That is a separate later
  decision (cloud-storage migration). Here we only ADD content as data.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.config import settings
from tender_agent.models import Tender, TenderDocumentContent, TenderDocumentFile

from .document_extractor import (
    EXTRACTOR_VERSION,
    ExtractedContent,
    extract_file_path,
)

logger = structlog.get_logger(__name__)


@dataclass
class ContentExtractionResult:
    """Summary of one ensure_content_extracted run. The reused counter is the
    proof point for 'never re-extract identical content': across re-fetches and
    across tenders, identical sha256 + extractor_version content is loaded
    from the DB, not re-read from disk."""

    contents: list[TenderDocumentContent]
    extracted: int  # newly run through the extractor
    reused_same_tender: int  # row already existed for this (file, sha, version)
    reused_cross_tender: int  # text copied from another tender's content row
    failed: int  # extraction status != ok and status != empty


def _resolve_storage_path(storage_key: str) -> Path:
    """Resolve a storage_key to a real on-disk path under DOCUMENT_STORAGE_DIR.

    Mirrors the path layout used by `_persist_documents` and the
    serve_document_file endpoint. The path-traversal guard there checks that
    the resolved file lives under the storage root; we apply the same check
    so the extractor can't be tricked into reading an arbitrary file.
    """
    root = Path(settings.document_storage_dir).resolve()
    candidate = (root / storage_key).resolve()
    if not str(candidate).startswith(str(root) + os.sep) and candidate != root:
        raise ValueError(f"storage_key escapes storage root: {storage_key}")
    return candidate


def _find_existing_for_file(
    db: Session, doc_file: TenderDocumentFile, sha: str
) -> TenderDocumentContent | None:
    """Existing row for THIS file + current extractor version."""
    return db.execute(
        select(TenderDocumentContent)
        .where(TenderDocumentContent.document_file_id == doc_file.id)
        .where(TenderDocumentContent.sha256 == sha)
        .where(TenderDocumentContent.extractor_version == EXTRACTOR_VERSION)
        .limit(1)
    ).scalar_one_or_none()


def _find_existing_by_sha(
    db: Session, sha: str
) -> TenderDocumentContent | None:
    """Existing row for the SAME sha256 + extractor version on ANY tender.
    This is what makes 'extract once, reuse forever' work across tenders.
    """
    return db.execute(
        select(TenderDocumentContent)
        .where(TenderDocumentContent.sha256 == sha)
        .where(TenderDocumentContent.extractor_version == EXTRACTOR_VERSION)
        .limit(1)
    ).scalar_one_or_none()


def _row_from_extraction(
    *,
    doc_file: TenderDocumentFile,
    tender_id: int,
    sha: str,
    extracted: ExtractedContent,
) -> TenderDocumentContent:
    return TenderDocumentContent(
        document_file_id=doc_file.id,
        tender_id=tender_id,
        sha256=sha,
        extracted_text=extracted.text,
        char_count=extracted.char_count,
        extraction_status=extracted.status,
        extraction_detail=extracted.detail,
        doc_type=extracted.doc_type,
        extractor_version=EXTRACTOR_VERSION,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def ensure_content_extracted(
    db: Session, tender: Tender
) -> ContentExtractionResult:
    """For each downloaded document of `tender`, make sure there's a
    `tender_document_content` row for (sha256, current extractor_version).

    Reuse rules:
    1. A row already exists for THIS file + current extractor → no work.
    2. A row exists for the SAME sha256 + current extractor on a DIFFERENT
       file (typically a different tender) → COPY the extracted_text into a
       row for this tender (no disk read, no re-extraction).
    3. Otherwise → read the file from disk and extract.

    Per-file failures are recorded as status="error" rows and do NOT raise.
    This makes the call safe to invoke from the fetch flow (extraction
    failures must not break a successful download) and from brief
    generation (idempotent safety net).
    """
    files: list[TenderDocumentFile] = list(tender.document_files or [])
    out: list[TenderDocumentContent] = []
    extracted = reused_same = reused_cross = failed = 0

    for f in files:
        if not f.sha256:
            # Files without a sha256 (e.g. failed downloads) have nothing to
            # extract from — skip silently. They'll appear in
            # tender_document_files with download_status="error".
            continue

        # 1. Same file + same sha + same extractor version → already done.
        existing = _find_existing_for_file(db, f, f.sha256)
        if existing is not None:
            out.append(existing)
            reused_same += 1
            continue

        # 2. Identical content seen elsewhere (same sha256, current extractor
        #    version) → the existing row IS the canonical extraction; we just
        #    reference it for this tender. No new row, no disk read, no
        #    re-extraction. Per the (sha256, extractor_version) unique
        #    constraint, only one extraction exists globally for a given byte
        #    blob; per-tender listings join on sha256 (see
        #    api/tenders.py::get_documents).
        twin = _find_existing_by_sha(db, f.sha256)
        if twin is not None:
            out.append(twin)
            reused_cross += 1
            continue

        # 3. Fresh extraction. Read the file once, store the text in the DB,
        #    then the next access for any tender goes through path 1 or 2.
        if not f.storage_key:
            row = _row_from_extraction(
                doc_file=f,
                tender_id=tender.id,
                sha=f.sha256,
                extracted=ExtractedContent(
                    text=None,
                    char_count=0,
                    status="error",
                    detail="no storage_key on document file",
                    doc_type=f.format,
                ),
            )
            db.add(row)
            db.flush()
            out.append(row)
            failed += 1
            continue

        try:
            path = _resolve_storage_path(f.storage_key)
        except Exception as exc:  # noqa: BLE001
            row = _row_from_extraction(
                doc_file=f,
                tender_id=tender.id,
                sha=f.sha256,
                extracted=ExtractedContent(
                    text=None,
                    char_count=0,
                    status="error",
                    detail=f"path resolve failed: {exc}",
                    doc_type=f.format,
                ),
            )
            db.add(row)
            db.flush()
            out.append(row)
            failed += 1
            continue

        extracted_result = extract_file_path(
            path,
            filename=f.title or path.name,
            content_type=None,
        )
        row = _row_from_extraction(
            doc_file=f,
            tender_id=tender.id,
            sha=f.sha256,
            extracted=extracted_result,
        )
        db.add(row)
        db.flush()
        out.append(row)
        extracted += 1
        if extracted_result.status not in ("ok", "empty"):
            failed += 1

    db.commit()
    logger.info(
        "brief.content_extracted",
        tender_id=tender.id,
        files=len(files),
        extracted=extracted,
        reused=reused_same + reused_cross,
        reused_same_tender=reused_same,
        reused_cross_tender=reused_cross,
        failed=failed,
        extractor_version=EXTRACTOR_VERSION,
    )
    return ContentExtractionResult(
        contents=out,
        extracted=extracted,
        reused_same_tender=reused_same,
        reused_cross_tender=reused_cross,
        failed=failed,
    )
