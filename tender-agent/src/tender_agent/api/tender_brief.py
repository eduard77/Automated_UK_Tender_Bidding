"""POST /tenders/{id}/generate-brief and GET /tenders/{id}/brief.

Mirrors the fetch-documents async-task pattern: the POST returns 202 with the
brief row id; a background task does the work; the GET returns the latest brief.

Regenerate = another POST. History is kept (older briefs stay in the DB), but
the GET always returns the most recent row.
"""
from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.db import SessionLocal, get_db
from tender_agent.models import Tender, TenderBrief
from tender_agent.schemas import TenderBriefRead
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
    db: Session = Depends(get_db),
) -> TenderBriefRead:
    tender = db.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(status_code=404, detail="tender not found")
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
    background_tasks.add_task(schedule_brief, brief.id, tender_id)
    return brief


@router.get("/{tender_id}/brief", response_model=TenderBriefRead | None)
def get_latest_brief(
    tender_id: int, db: Session = Depends(get_db)
) -> TenderBriefRead | None:
    tender = db.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(status_code=404, detail="tender not found")
    row = latest_brief(db, tender_id)
    if row is None:
        return None
    return row


@router.get("/{tender_id}/briefs", response_model=list[TenderBriefRead])
def list_briefs(
    tender_id: int, db: Session = Depends(get_db)
) -> list[TenderBriefRead]:
    """All briefs for a tender, newest first. Lets the dashboard show history
    if it wants to (chunk 5 only uses the latest)."""
    return list(
        db.execute(
            select(TenderBrief)
            .where(TenderBrief.tender_id == tender_id)
            .order_by(TenderBrief.created_at.desc())
        ).scalars().all()
    )
