"""POST /tenders/{id}/ingest-documents — operator-fetched documents → cloud.

Part of the local-fetch pivot: Delta is fetched on the operator's machine
(real browser, residential IP — the cloud IP is 403'd) and the results are
pushed UP here. This endpoint accepts each document's metadata + extracted text
+ raw bytes, stores the bytes in Azure Blob (managed identity), and writes the
tender_document_files / tender_document_content rows via `ingest_documents`.

Auth: `require_account` — same operator/account session-cookie auth as the other
operator endpoints (the local tool logs in via /accounts/login and reuses the
cookie, exactly like scripts/connect_delta.py). Anonymous callers get 401 before
any work.

Transport: multipart/form-data —
  * `metadata`  : a JSON string, a list of per-document objects (title, format,
                  content_type, sha256, extracted_text, char_count,
                  extraction_status, extraction_detail, doc_type,
                  extractor_version). Order matters.
  * `files`     : the document byte parts, in the SAME order as `metadata`.

Idempotent + deduped by sha256 (see services/portals/document_ingest.py).
"""
from __future__ import annotations

import json
from dataclasses import asdict

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from tender_agent.api.deps import require_account
from tender_agent.config import settings
from tender_agent.db import get_db
from tender_agent.models import Account, Tender
from tender_agent.services.portals.document_ingest import (
    ingest_documents,
    make_ingest_items,
)
from tender_agent.services.storage import get_storage_backend

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/tenders", tags=["tenders"])


@router.post("/{tender_id}/ingest-documents", status_code=200)
async def ingest_tender_documents(
    tender_id: int,
    metadata: str = Form(...),
    files: list[UploadFile] = File(default=[]),  # noqa: B008
    account: Account = Depends(require_account),
    db: Session = Depends(get_db),
) -> dict:
    """Ingest operator-fetched documents for a tender. Returns per-file outcomes
    and dedup/idempotency counts. NEVER trusts the client sha256 — bytes are
    re-hashed server-side."""
    tender = db.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(status_code=404, detail="tender not found")

    try:
        parsed = json.loads(metadata)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400, detail=f"metadata is not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, list) or not all(isinstance(m, dict) for m in parsed):
        raise HTTPException(
            status_code=400, detail="metadata must be a JSON list of objects"
        )

    # Read each uploaded file, enforcing the per-document size ceiling.
    files_by_index: dict[int, bytes] = {}
    for i, up in enumerate(files):
        data = await up.read()
        if len(data) > settings.ingest_max_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"file {i} is {len(data)} bytes, over the "
                    f"{settings.ingest_max_bytes}-byte per-document cap"
                ),
            )
        files_by_index[i] = data

    items = make_ingest_items(parsed, files_by_index)
    if not items:
        raise HTTPException(
            status_code=400,
            detail="no documents to ingest (no file parts matched the metadata)",
        )

    result = ingest_documents(
        db, tender_id, items, storage=get_storage_backend()
    )
    logger.info(
        "ingest.api", tender_id=tender_id, account_id=account.id,
        items=len(items), inserted=result.inserted, deduped=result.deduped,
    )
    return asdict(result)
