"""POST /tenders/{id}/generate-brief and GET /tenders/{id}/brief.

Chunk 6 wires server-side gating into BOTH endpoints:

* GET — anonymous / unentitled callers receive a 50% PREVIEW of the brief
  (allow-listed half + lock marker + counts). Entitled callers receive the
  full TenderBriefRead unchanged.
* POST — generation requires authentication. On a plan it consumes a
  generation (plan_100 only) and writes a `brief_entitlement(source='plan')`.
  PAYG / free accounts must buy first via /billing/checkout → the webhook
  creates the entitlement, then POST works.

The locked-content stripping happens in `services.accounts.entitlement`; this
module just decides which path to take.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.api.deps import current_account, require_account
from tender_agent.db import SessionLocal, get_db
from tender_agent.models import Account, Tender, TenderBrief
from tender_agent.schemas import TenderBriefRead
from tender_agent.services.accounts.entitlement import (
    PlanLimitReached,
    consume_plan_generation_if_new,
    grant_entitlement,
    has_entitlement_row,
    is_entitled,
    is_plan_active,
    redact_brief_json,
)
from tender_agent.services.brief.brief_generator import (
    generate_brief,
    latest_brief,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/tenders", tags=["tenders"])


async def schedule_brief(brief_id: int, tender_id: int) -> None:
    """Background runner. Tests monkeypatch this module attribute to a no-op
    so the TestClient never triggers a real LLM call."""
    with SessionLocal() as db:
        try:
            await generate_brief(db, tender_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "brief_task.failed", brief_id=brief_id, tender_id=tender_id
            )
            brief = db.get(TenderBrief, brief_id)
            if brief is not None and brief.status not in {"complete", "failed"}:
                brief.status = "failed"
                brief.error_detail = f"{type(exc).__name__}: {exc}"
                brief.generated_at = datetime.now(UTC)
                brief.updated_at = brief.generated_at
                db.commit()


@router.post(
    "/{tender_id}/generate-brief",
    status_code=202,
    response_model=TenderBriefRead,
)
def start_generate_brief(
    tender_id: int,
    background_tasks: BackgroundTasks,
    account: Account = Depends(require_account),
    db: Session = Depends(get_db),
) -> TenderBriefRead:
    """Auth required. Consumes a plan generation (plan_100 only) when the
    user is on a brief plan AND this tender doesn't yet have an entitlement.
    PAYG/free users with an existing entitlement (e.g. from a PAYG purchase)
    may regenerate freely.

    Failure modes:
    - 401: anonymous caller.
    - 402: no entitlement AND not on an active brief plan — they need to
      pay (single brief £10 OR subscribe).
    - 409 monthly_limit_reached: plan_100 has consumed all 100 generations.
    - 404: tender not found.
    """
    tender = db.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(status_code=404, detail="tender not found")

    on_active_plan = (
        account.plan in {"plan_100", "plan_unlimited"} and is_plan_active(account)
    )
    already_entitled = has_entitlement_row(
        db, account_id=account.id, tender_id=tender_id
    )

    if on_active_plan and not already_entitled:
        try:
            consume_plan_generation_if_new(
                db, account=account, tender_id=tender_id
            )
        except PlanLimitReached:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "monthly_limit_reached",
                    "message": (
                        "Monthly limit reached — upgrade to Unlimited or "
                        "buy a single brief (£10)."
                    ),
                },
            ) from None
    elif not already_entitled and not on_active_plan:
        raise HTTPException(
            status_code=402,
            detail={
                "code": "payment_required",
                "message": (
                    "Generating a brief requires a single brief purchase "
                    "(£10) or an active monthly plan."
                ),
            },
        )

    now = datetime.now(UTC)
    brief = TenderBrief(
        tender_id=tender_id,
        status="generating",
        created_at=now,
        updated_at=now,
    )
    db.add(brief)
    db.commit()
    db.refresh(brief)
    # plan_unlimited bypasses the per-row entitlement check for viewing,
    # but we still want an audit-trail row. Idempotent.
    if account.plan == "plan_unlimited" and is_plan_active(account):
        grant_entitlement(
            db, account=account, tender_id=tender_id, source="plan"
        )
    background_tasks.add_task(schedule_brief, brief.id, tender_id)
    return brief


def _redacted_brief_read(row: TenderBrief) -> dict[str, Any]:
    """Build the half-preview response. We do NOT pass through the SA model
    via TenderBriefRead.model_validate, because we have to redact several
    fields — instead we hand-build the dict matching TenderBriefRead.
    """
    return {
        "id": row.id,
        "tender_id": row.tender_id,
        "status": row.status,
        # The locked fields are NULLED OUT on the wire so an unentitled
        # client physically cannot read them. The preview lives in
        # brief_json under brief_json["locked"]=True.
        "recommendation": None,
        "confidence": None,
        "headline": row.headline,
        "brief_json": redact_brief_json(row.brief_json),
        "model": row.model,
        "documents_considered": None,
        "input_tokens": None,
        "output_tokens": None,
        "error_detail": None,
        "generated_at": row.generated_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


@router.get(
    "/{tender_id}/brief",
    response_model=TenderBriefRead | None,
)
def get_latest_brief(
    tender_id: int,
    account: Account | None = Depends(current_account),
    db: Session = Depends(get_db),
):
    """Anonymous + unentitled => preview. Entitled => full row.

    The response NEVER carries locked content for the unentitled — assertions
    in the test suite verify that the recommendation / rationale text is
    physically absent from the response body.
    """
    tender = db.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(status_code=404, detail="tender not found")
    row = latest_brief(db, tender_id)
    if row is None:
        return None
    if is_entitled(db, account=account, tender_id=tender_id):
        return row
    return _redacted_brief_read(row)


@router.get("/{tender_id}/briefs", response_model=list[TenderBriefRead])
def list_briefs(
    tender_id: int,
    account: Account | None = Depends(current_account),
    db: Session = Depends(get_db),
):
    """History view. Same gating as the single brief — locked content is
    redacted per-row for unentitled callers."""
    rows = list(
        db.execute(
            select(TenderBrief)
            .where(TenderBrief.tender_id == tender_id)
            .order_by(TenderBrief.created_at.desc())
        ).scalars().all()
    )
    if is_entitled(db, account=account, tender_id=tender_id):
        return rows
    return [_redacted_brief_read(r) for r in rows]
