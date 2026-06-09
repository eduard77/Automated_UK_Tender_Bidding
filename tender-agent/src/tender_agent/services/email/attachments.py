"""File email attachments against a tender via the EXISTING ingest path.

We do NOT fork document storage. Each attachment is text-extracted with the
same extractor the brief engine uses, wrapped in an `IngestItem`, and handed to
`ingest_documents` — which writes the bytes to the configured storage backend
and upserts the DB rows, deduped by (tender_id, sha256) and content-deduped by
(sha256, extractor_version). Re-filing the same attachment is idempotent.

Only attachments are pulled. Links in the body are never fetched (see poller).
"""
from __future__ import annotations

import structlog
from sqlalchemy.orm import Session

from tender_agent.services.brief.document_extractor import (
    EXTRACTOR_VERSION,
    extract_from_bytes,
)
from tender_agent.services.email.providers.base import EmailAttachment
from tender_agent.services.portals.document_ingest import (
    IngestItem,
    IngestResult,
    ingest_documents,
)
from tender_agent.services.storage import get_storage_backend

logger = structlog.get_logger(__name__)


def _ext_of(filename: str) -> str:
    if filename and "." in filename:
        cand = filename.rsplit(".", 1)[-1].lower()
        if cand.isalnum():
            return cand
    return ""


def _to_ingest_item(att: EmailAttachment) -> tuple[IngestItem, bytes]:
    ext = _ext_of(att.filename)
    extracted = extract_from_bytes(att.data, ext, filename=att.filename)
    item = IngestItem(
        title=att.filename,
        format=ext or None,
        content_type=att.content_type,
        extracted_text=extracted.text,
        char_count=extracted.char_count,
        extraction_status=extracted.status,
        extraction_detail=extracted.detail,
        doc_type=extracted.doc_type,
        extractor_version=EXTRACTOR_VERSION,
    )
    return item, att.data


def file_email_attachments(
    db: Session, tender_id: int, attachments: list[EmailAttachment]
) -> IngestResult:
    """File every attachment against `tender_id` via the existing ingest path.

    Returns the IngestResult (insert/dedup/content counts). An empty attachment
    list yields a zero-count result without touching storage.
    """
    if not attachments:
        return IngestResult(tender_id=tender_id)
    items = [_to_ingest_item(att) for att in attachments]
    result = ingest_documents(
        db, tender_id, items, storage=get_storage_backend()
    )
    logger.info(
        "email.attachments_filed",
        tender_id=tender_id,
        count=len(attachments),
        inserted=result.inserted,
        deduped=result.deduped,
    )
    return result
