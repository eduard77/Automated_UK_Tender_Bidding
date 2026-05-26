"""Tender query endpoints."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.db import get_db
from tender_agent.models import (
    FilterMatch,
    Tender,
    TenderDocumentContent,
    TenderDocumentFile,
    TenderRequirements,
)
from tender_agent.schemas import (
    TenderDocumentFileRead,
    TenderRead,
    TenderRequirementsRead,
)
from tender_agent.services.brief.content_store import EXTRACTOR_VERSION

router = APIRouter(prefix="/tenders", tags=["tenders"])


@router.get("", response_model=list[TenderRead])
def list_tenders(
    db: Session = Depends(get_db),
    source: str | None = Query(None, description="Filter by source_code (FTS, CF, ...)"),
    buyer: str | None = Query(None),
    status: str | None = Query(None),
    deadline_after: datetime | None = Query(None),
    matched_only: bool = Query(False, description="Only include tenders that matched a filter"),
    include_duplicates: bool = Query(False),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[TenderRead]:
    stmt = select(Tender)
    if source:
        stmt = stmt.where(Tender.source_code == source)
    if buyer:
        stmt = stmt.where(Tender.buyer_name.ilike(f"%{buyer}%"))
    if status:
        stmt = stmt.where(Tender.status == status)
    if deadline_after:
        stmt = stmt.where(Tender.deadline_at >= deadline_after)
    if not include_duplicates:
        stmt = stmt.where(Tender.duplicate_of_id.is_(None))
    if matched_only:
        stmt = stmt.join(FilterMatch, FilterMatch.tender_id == Tender.id).distinct()

    stmt = stmt.order_by(Tender.published_at.desc().nullslast()).offset(offset).limit(limit)
    return list(db.execute(stmt).scalars().all())


@router.get("/{tender_id}", response_model=TenderRead)
def get_tender(tender_id: int, db: Session = Depends(get_db)) -> TenderRead:
    tender = db.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(status_code=404, detail="tender not found")
    return tender


@router.get("/{tender_id}/requirements", response_model=TenderRequirementsRead)
def get_requirements(tender_id: int, db: Session = Depends(get_db)) -> TenderRequirementsRead:
    req = db.execute(
        select(TenderRequirements).where(TenderRequirements.tender_id == tender_id)
    ).scalar_one_or_none()
    if req is None:
        raise HTTPException(status_code=404, detail="no requirements extracted yet")
    return req


@router.get("/{tender_id}/documents", response_model=list[TenderDocumentFileRead])
def get_documents(
    tender_id: int, db: Session = Depends(get_db)
) -> list[TenderDocumentFileRead]:
    files = list(
        db.execute(
            select(TenderDocumentFile)
            .where(TenderDocumentFile.tender_id == tender_id)
            .order_by(TenderDocumentFile.created_at)
        ).scalars().all()
    )
    # Which files have a usable, current-version content row? Joined here so
    # the dashboard can show a "content stored" indicator without a second
    # round-trip per file.
    shas = {f.sha256 for f in files if f.sha256}
    stored: set[str] = set()
    if shas:
        stored = {
            s
            for (s,) in db.execute(
                select(TenderDocumentContent.sha256)
                .where(TenderDocumentContent.sha256.in_(shas))
                .where(TenderDocumentContent.extractor_version == EXTRACTOR_VERSION)
                .where(TenderDocumentContent.extraction_status == "ok")
            ).all()
        }
    out: list[TenderDocumentFileRead] = []
    for f in files:
        row = TenderDocumentFileRead.model_validate(f)
        row.content_stored = bool(f.sha256 and f.sha256 in stored)
        out.append(row)
    return out
