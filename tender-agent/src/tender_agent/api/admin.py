"""Admin endpoints — operator-only utilities for manually triggering extraction
and other one-shot operations. Not user-facing.

Existing admin routes live in `api/filters.py` (`/admin/poll-now`) for legacy
reasons; new admin endpoints land here.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from tender_agent.config import settings
from tender_agent.db import get_db
from tender_agent.models import Tender
from tender_agent.schemas import TenderRequirementsRead
from tender_agent.services.requirements_extractor import extract_requirements

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post(
    "/extract-requirements/{tender_id}",
    response_model=TenderRequirementsRead,
)
def trigger_extract_requirements(
    tender_id: int, db: Session = Depends(get_db)
) -> TenderRequirementsRead:
    """Re-run requirements extraction for a single tender, overwriting any
    existing TenderRequirements row.

    Used by:
    - the dashboard's "Generate brief" button on the tender detail page
    - the `scripts/validate_extractor.py` validation harness
    - operators investigating a specific tender by hand

    Status codes:
    - 200: extraction succeeded, returns the new TenderRequirementsRead.
    - 404: tender does not exist.
    - 422: tender has no description AND no downloaded documents — there's
      nothing to extract from. Run the document downloader first.
    - 503: ANTHROPIC_API_KEY is not configured on the backend.
    - 502: the extractor was called but failed (Claude error, JSON parse error,
      etc.). Check structured logs for `requirements.api_error` /
      `requirements.parse_failed`.
    """
    tender = db.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(status_code=404, detail="tender not found")

    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=503,
            detail="anthropic_api_key not configured on the backend",
        )

    has_description = bool(tender.description and tender.description.strip())
    has_documents = bool(tender.document_files)
    if not has_description and not has_documents:
        raise HTTPException(
            status_code=422,
            detail=(
                "tender has no description and no downloaded documents — "
                "nothing to extract from"
            ),
        )

    record = extract_requirements(db, tender)
    if record is None:
        # extract_requirements returns None on Anthropic API error or JSON parse
        # failure. The structured log line is the source of truth; the operator
        # gets a generic 502 here.
        raise HTTPException(
            status_code=502,
            detail="extractor failed — check logs for requirements.api_error / parse_failed",
        )
    return record
