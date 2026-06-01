"""Tender query endpoints."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.api.deps import current_account
from tender_agent.db import get_db
from tender_agent.models import (
    Account,
    FilterMatch,
    Tender,
    TenderDocumentContent,
    TenderDocumentFile,
    TenderRequirements,
)
from tender_agent.schemas import (
    RegionFacet,
    TenderDocumentFileRead,
    TenderFacets,
    TenderRead,
    TenderRequirementsRead,
    TenderSearchResponse,
    TenderSearchResult,
)
from tender_agent.services.accounts.entitlement import (
    half_preview_text,
    is_entitled,
)
from tender_agent.services.brief.content_store import EXTRACTOR_VERSION
from tender_agent.services.tender_search import (
    SortKey,
    TenderSearchParams,
    search_tenders,
    tender_facets,
)

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


@router.get("/search", response_model=TenderSearchResponse)
def search(
    db: Session = Depends(get_db),
    q: str | None = Query(None, description="Free text across title, description, keywords"),
    cpv: list[str] = Query(default_factory=list, description="CPV codes; match any overlap"),
    region: list[str] = Query(
        default_factory=list, description="canonical region (exact, OR; see /tenders/facets)"
    ),
    buyer: str | None = Query(None, description="buyer_name contains"),
    value_min: Decimal | None = Query(None, ge=0),
    value_max: Decimal | None = Query(None, ge=0),
    value_stated_only: bool = Query(
        False, description="With a value range, exclude null-value tenders (default: include)"
    ),
    deadline_from: datetime | None = Query(None),
    deadline_to: datetime | None = Query(None),
    open_only: bool = Query(False, description="Only live notices: future deadline + active"),
    status: list[str] = Query(default_factory=list),
    source: list[str] = Query(default_factory=list, description="source_code; default all"),
    include_duplicates: bool = Query(
        True, description="Show all matches; duplicates are annotated, not hidden"
    ),
    sort: SortKey = Query(SortKey.deadline_asc),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
) -> TenderSearchResponse:
    """Source-agnostic search over the tender table. See services/tender_search."""
    params = TenderSearchParams(
        q=q,
        cpv=cpv,
        region=region,
        buyer=buyer,
        value_min=value_min,
        value_max=value_max,
        value_stated_only=value_stated_only,
        deadline_from=deadline_from,
        deadline_to=deadline_to,
        open_only=open_only,
        status=status,
        source=source,
        include_duplicates=include_duplicates,
        sort=sort,
        page=page,
        page_size=page_size,
    )
    total, rows = search_tenders(db, params)
    results = []
    for t in rows:
        row = TenderSearchResult.model_validate(t)
        row.is_duplicate = t.duplicate_of_id is not None
        results.append(row)
    return TenderSearchResponse(
        total=total, page=page, page_size=page_size, results=results
    )


@router.get("/facets", response_model=TenderFacets)
def facets(db: Session = Depends(get_db)) -> TenderFacets:
    """Distinct sources/statuses/regions for the search filter controls."""
    sources, statuses, regions = tender_facets(db)
    return TenderFacets(
        sources=sources,
        statuses=statuses,
        regions=[RegionFacet(value=value, count=count) for value, count in regions],
    )


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


@router.get("/{tender_id}/documents/{doc_id}/content")
def get_document_content(
    tender_id: int,
    doc_id: int,
    account: Account | None = Depends(current_account),
    db: Session = Depends(get_db),
) -> dict:
    """Extracted-text view (chunk 6 gate).

    Entitled callers receive the FULL extracted text plus `locked=False`.
    Anonymous / unentitled callers receive the first 50% of the text with a
    lock marker appended and `locked=True`. The full text is never sent to
    an unentitled client — test suite asserts the second-half substring is
    physically absent from the response body.
    """
    doc = db.get(TenderDocumentFile, doc_id)
    if doc is None or doc.tender_id != tender_id:
        raise HTTPException(status_code=404, detail="document not found")

    # Prefer the canonical TenderDocumentContent row by (sha, extractor_version);
    # fall back to the file row's inline text_extracted if no content row yet.
    text: str | None = None
    if doc.sha256:
        row = db.execute(
            select(TenderDocumentContent)
            .where(TenderDocumentContent.sha256 == doc.sha256)
            .where(TenderDocumentContent.extractor_version == EXTRACTOR_VERSION)
            .order_by(TenderDocumentContent.created_at.desc())
        ).scalars().first()
        if row is not None:
            text = row.extracted_text
    if text is None:
        text = doc.text_extracted

    full_chars = len(text) if text else 0
    if is_entitled(db, account=account, tender_id=tender_id):
        return {
            "doc_id": doc_id,
            "tender_id": tender_id,
            "title": doc.title,
            "format": doc.format,
            "locked": False,
            "char_count": full_chars,
            "preview_chars": full_chars,
            "text": text or "",
        }
    preview, _truncated = half_preview_text(text)
    return {
        "doc_id": doc_id,
        "tender_id": tender_id,
        "title": doc.title,
        "format": doc.format,
        "locked": True,
        "char_count": full_chars,
        "preview_chars": len(preview),
        "text": preview,
        "unlock": {
            "reason": "document_locked",
            "message": (
                "Document is locked. Buy a single brief (£10) or subscribe "
                "to read the full text and download."
            ),
        },
    }
