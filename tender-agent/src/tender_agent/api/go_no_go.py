"""Go/No-Go API — advisory warnings + reconciliation.

Two endpoints, both read-only:
    GET /tenders/{id}/go-no-go            — rating + red warnings + missing
                                            info + self-cert questions
    GET /tenders/{id}/vault-reconciliation — vault evidence vs mandatory
                                             requirements

Both depend ONLY on the brief + the vault — no payments, no accounts, no
cloud DB. If the tender has no complete brief yet they return a clean
"generate a brief first" 409 state rather than 500.

Per the spec — these endpoints NEVER block anything. They emit warnings
and questions; the client decides.
"""
from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from tender_agent.db import get_db
from tender_agent.models import Tender
from tender_agent.services.go_no_go import (
    assess_tender,
    reconcile_vault_against_tender,
)

# The engine itself fetches the brief; this import is only used by the
# vault-reconciliation endpoint to load the latest complete brief.
from tender_agent.services.go_no_go.engine import BriefNotReady, _latest_complete_brief
from tender_agent.services.go_no_go.reconciliation import (  # noqa: F401
    ReconciliationResult,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/tenders", tags=["go-no-go"])


@router.get("/{tender_id}/go-no-go")
def get_go_no_go(
    tender_id: int,
    org_id: int = Query(1, ge=1, description="Tenant scope for the vault read"),
    db: Session = Depends(get_db),
) -> dict:
    tender = db.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(status_code=404, detail="tender_not_found")
    try:
        result = assess_tender(db, tender, org_id=org_id)
    except BriefNotReady:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "brief_not_ready",
                "message": (
                    "Generate a brief for this tender first — the go-no-go "
                    "engine reads the brief, it doesn't reproduce it."
                ),
            },
        ) from None
    return result.to_dict()


@router.get("/{tender_id}/vault-reconciliation")
def get_vault_reconciliation(
    tender_id: int,
    org_id: int = Query(1, ge=1),
    db: Session = Depends(get_db),
) -> dict:
    """Word-vs-evidence check. Re-runnable on demand — a contradiction
    resolved by a later vault upload simply stops being emitted next time."""
    tender = db.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(status_code=404, detail="tender_not_found")
    brief = _latest_complete_brief(db, tender.id)
    if brief is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "brief_not_ready",
                "message": "Generate a brief for this tender first.",
            },
        )
    result = reconcile_vault_against_tender(
        db, tender=tender, brief=brief, org_id=org_id
    )
    return result.to_dict()
