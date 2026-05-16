"""Vault API — minimal HTTP surface over the vault tables.

This module ships the v1 routes needed by the dashboard. It is intentionally
thin: it reads/writes the vault_documents and vault_document_versions tables
and stores blobs via services.vault.storage. The classifier + extractors that
turn an uploaded PDF into structured claims are NOT wired here — that's a
follow-up. Upload accepts files, stores them, and creates a version row with
empty `claims` and `claims_confirmed=False`. The dashboard's Edit claims form
lets a user fill those in by hand until auto-extraction lands.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from tender_agent.db import get_db
from tender_agent.models import VaultDocument, VaultDocumentVersion
from tender_agent.services.vault.storage import get_storage

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/vault", tags=["vault"])

# ---------------------------------------------------------------------------
# Response/request models
# ---------------------------------------------------------------------------


class VaultDocumentVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    document_id: int
    version: int
    storage_key: str
    bytes: int
    sha256: str
    mime_type: str
    title: str
    expiry_date: date | None = None
    issuing_body: str | None = None
    issued_date: date | None = None
    last_reviewed_date: date | None = None
    superseded_by_version_id: int | None = None
    claims: dict[str, Any] = Field(default_factory=dict)
    claims_confirmed: bool = False
    text_extracted: str | None = None
    uploaded_by: str | None = None
    uploaded_at: datetime


class VaultDocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    category: str
    subcategory: str | None = None
    title: str
    owner_email: str | None = None
    confidentiality: str
    created_at: datetime
    current_version: VaultDocumentVersionRead | None = None


class ClaimsUpdate(BaseModel):
    """PATCH body for /vault/documents/{id}/versions/{vid}/claims.

    `claims` is the full new payload — we replace, we don't merge. The
    `expiry_date`, `issuing_body`, `issued_date` mirror fields can be lifted
    out of the claims (caller's choice) and stored as columns for cheap
    WHERE filtering by the matcher and the /vault/expiring endpoint.
    """

    claims: dict[str, Any]
    claims_confirmed: bool | None = None
    expiry_date: date | None = None
    issuing_body: str | None = None
    issued_date: date | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VAULT_CATEGORIES = {
    "corporate",
    "financial",
    "insurance",
    "accreditation",
    "policy",
    "capability",
    "people",
    "technical",
    "past_bid",
    "uncategorised",
}


def _ext_from_mime(mime: str) -> str:
    if "pdf" in mime:
        return "pdf"
    if "word" in mime or "officedocument" in mime:
        return "docx"
    if "text" in mime:
        return "txt"
    return "bin"


def _load_document(db: Session, document_id: int) -> VaultDocument:
    doc = (
        db.execute(
            select(VaultDocument)
            .options(selectinload(VaultDocument.current_version))
            .where(VaultDocument.id == document_id, VaultDocument.deleted_at.is_(None))
        )
        .scalars()
        .one_or_none()
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="vault document not found")
    return doc


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/documents", response_model=list[VaultDocumentRead])
def list_documents(
    category: str | None = None,
    include_deleted: bool = False,
    db: Session = Depends(get_db),
) -> list[VaultDocumentRead]:
    stmt = (
        select(VaultDocument)
        .options(selectinload(VaultDocument.current_version))
        .order_by(VaultDocument.created_at.desc())
    )
    if not include_deleted:
        stmt = stmt.where(VaultDocument.deleted_at.is_(None))
    if category is not None:
        stmt = stmt.where(VaultDocument.category == category)
    rows = db.execute(stmt).scalars().all()
    return [VaultDocumentRead.model_validate(d) for d in rows]


@router.get("/documents/{document_id}", response_model=VaultDocumentRead)
def get_document(document_id: int, db: Session = Depends(get_db)) -> VaultDocumentRead:
    doc = _load_document(db, document_id)
    return VaultDocumentRead.model_validate(doc)


@router.post(
    "/documents",
    response_model=VaultDocumentRead,
    status_code=201,
)
def upload_document(
    file: UploadFile = File(...),
    description: str = Form(...),
    title: str | None = Form(None),
    category: str = Form("uncategorised"),
    db: Session = Depends(get_db),
) -> VaultDocumentRead:
    """Accept a file + free-text description and create a vault document.

    The v1 implementation stores the blob and creates a version row with empty
    claims. The dashboard surfaces an Edit-claims form so the user can fill in
    the structured claim values manually. Auto-extraction via Claude is a
    follow-up; when it lands, this route becomes the wire-up point.
    """
    if category not in VAULT_CATEGORIES:
        raise HTTPException(
            status_code=422, detail=f"invalid category '{category}'"
        )

    content = file.file.read()
    if not content:
        raise HTTPException(status_code=400, detail="empty file")

    final_title = (title or file.filename or "Untitled document").strip()

    doc = VaultDocument(
        category=category,
        title=final_title,
    )
    db.add(doc)
    db.flush()  # assign doc.id for the storage key path

    ext = _ext_from_mime(file.content_type or "")
    blob = get_storage().save(
        org_id=doc.org_id,
        document_id=doc.id,
        version=1,
        ext=ext,
        content=content,
    )

    version = VaultDocumentVersion(
        document_id=doc.id,
        version=1,
        storage_key=blob.storage_key,
        bytes=blob.bytes,
        sha256=blob.sha256,
        mime_type=file.content_type or "application/octet-stream",
        title=final_title,
        claims={
            "doc_type": "insurance_certificate",
            "low_confidence_fields": [],
            "notes": [description],
        },
        claims_confirmed=False,
    )
    db.add(version)
    db.flush()

    doc.current_version_id = version.id
    db.commit()
    db.refresh(doc)
    # Eager-load the relationship for the response.
    db.expire(doc)
    doc = _load_document(db, doc.id)
    logger.info(
        "vault.upload.created",
        document_id=doc.id,
        version_id=version.id,
        bytes=blob.bytes,
        category=category,
    )
    return VaultDocumentRead.model_validate(doc)


@router.patch(
    "/documents/{document_id}/versions/{version_id}/claims",
    response_model=VaultDocumentRead,
)
def update_claims(
    document_id: int,
    version_id: int,
    payload: ClaimsUpdate,
    db: Session = Depends(get_db),
) -> VaultDocumentRead:
    version = db.get(VaultDocumentVersion, version_id)
    if version is None or version.document_id != document_id:
        raise HTTPException(status_code=404, detail="version not found")
    version.claims = payload.claims
    if payload.claims_confirmed is not None:
        version.claims_confirmed = payload.claims_confirmed
    if payload.expiry_date is not None:
        version.expiry_date = payload.expiry_date
    if payload.issuing_body is not None:
        version.issuing_body = payload.issuing_body
    if payload.issued_date is not None:
        version.issued_date = payload.issued_date
    db.commit()
    return VaultDocumentRead.model_validate(_load_document(db, document_id))


@router.post(
    "/documents/{document_id}/supersede",
    response_model=VaultDocumentRead,
)
def supersede(
    document_id: int,
    file: UploadFile = File(...),
    description: str = Form(""),
    db: Session = Depends(get_db),
) -> VaultDocumentRead:
    doc = _load_document(db, document_id)
    content = file.file.read()
    if not content:
        raise HTTPException(status_code=400, detail="empty file")

    prev_version = doc.current_version
    next_version_number = (prev_version.version + 1) if prev_version else 1

    ext = _ext_from_mime(file.content_type or "")
    blob = get_storage().save(
        org_id=doc.org_id,
        document_id=doc.id,
        version=next_version_number,
        ext=ext,
        content=content,
    )

    new_version = VaultDocumentVersion(
        document_id=doc.id,
        version=next_version_number,
        storage_key=blob.storage_key,
        bytes=blob.bytes,
        sha256=blob.sha256,
        mime_type=file.content_type or "application/octet-stream",
        title=doc.title,
        # Carry the previous version's claims forward as a starting point;
        # caller PATCHes to override fields that actually changed.
        claims=prev_version.claims if prev_version else {},
        claims_confirmed=False,
    )
    db.add(new_version)
    db.flush()

    if prev_version is not None:
        prev_version.superseded_by_version_id = new_version.id
    doc.current_version_id = new_version.id
    db.commit()
    db.refresh(doc)
    return VaultDocumentRead.model_validate(_load_document(db, document_id))


@router.get("/expiring", response_model=list[VaultDocumentRead])
def expiring_within(
    days: int = 30,
    db: Session = Depends(get_db),
) -> list[VaultDocumentRead]:
    """Documents whose current version expires within `days` from today."""
    if days < 0:
        raise HTTPException(status_code=422, detail="days must be >= 0")
    cutoff = date.today()
    from datetime import timedelta

    horizon = cutoff + timedelta(days=days)
    rows = (
        db.execute(
            select(VaultDocument)
            .join(
                VaultDocumentVersion,
                VaultDocumentVersion.id == VaultDocument.current_version_id,
            )
            .options(selectinload(VaultDocument.current_version))
            .where(
                VaultDocument.deleted_at.is_(None),
                VaultDocumentVersion.expiry_date.is_not(None),
                VaultDocumentVersion.expiry_date <= horizon,
            )
            .order_by(VaultDocumentVersion.expiry_date.asc())
        )
        .scalars()
        .all()
    )
    return [VaultDocumentRead.model_validate(d) for d in rows]


@router.delete("/documents/{document_id}", status_code=204)
def archive_document(
    document_id: int,
    db: Session = Depends(get_db),
) -> None:
    """Soft-delete a vault document. Per PROJECT.md §7 we never hard-delete —
    bids cite specific version IDs. Archiving sets deleted_at; the rows stay."""
    doc = _load_document(db, document_id)
    doc.deleted_at = datetime.now(UTC)
    db.commit()
