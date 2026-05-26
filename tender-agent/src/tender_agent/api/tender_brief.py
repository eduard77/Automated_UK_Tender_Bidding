"""POST /tenders/{id}/generate-brief and GET /tenders/{id}/brief.

Mirrors the fetch-documents async-task pattern: POST creates a
TenderBrief row in status='generating', schedules a background task to
extract content (idempotent), call the LLM, validate, and persist; GET
returns the latest brief for the tender.

The background runner is referenced by name (`schedule_generate_brief`) so
tests can monkeypatch it to a no-op to keep CI deterministic and offline.
"""
from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.db import SessionLocal, get_db
from tender_agent.models import Tender, TenderBrief
from tender_agent.schemas import TenderBriefRead, TenderBriefStarted
from tender_agent.services.brief.brief_generator import generate_brief
from tender_agent.services.brief.llm_client import (
    BriefLLMClient,
    LLMConfigError,
    get_default_client,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/tenders", tags=["briefs"])


# Tests monkeypatch this to inject a fake LLM. Default returns the
# Anthropic-backed client which reads ANTHROPIC_API_KEY at call time.
def _llm_client_factory() -> BriefLLMClient:
    return get_default_client()


async def schedule_generate_brief(brief_id: int, tender_id: int) -> None:
    """Background runner. Loads the tender + brief, runs generation, writes
    the result. Tests monkeypatch this module attribute to a no-op so the
    TestClient never triggers a real Anthropic call."""
    with SessionLocal() as db:
        tender = db.get(Tender, tender_id)
        if tender is None:
            logger.warning(
                "brief.bg_tender_missing", tender_id=tender_id, brief_id=brief_id
            )
            return
        # Drop the placeholder row created by the POST handler — generate_brief
        # always inserts a fresh row for clean accounting. Keeping the
        # placeholder around would leave a stale "generating" sibling.
        placeholder = db.get(TenderBrief, brief_id)
        if placeholder is not None:
            db.delete(placeholder)
            db.commit()

        try:
            llm = _llm_client_factory()
            await generate_brief(db, tender, llm=llm)
        except LLMConfigError as exc:
            # No key set — generator wouldn't even insert; record one for the UI.
            now = datetime.now(UTC)
            fail = TenderBrief(
                tender_id=tender.id,
                status="failed",
                error_detail=str(exc),
                created_at=now,
                updated_at=now,
            )
            db.add(fail)
            db.commit()
            logger.warning(
                "brief.bg_config_error", tender_id=tender.id, error=str(exc)
            )
        except Exception as exc:  # noqa: BLE001
            now = datetime.now(UTC)
            fail = TenderBrief(
                tender_id=tender.id,
                status="failed",
                error_detail=f"{type(exc).__name__}: {exc}",
                created_at=now,
                updated_at=now,
            )
            db.add(fail)
            db.commit()
            logger.exception(
                "brief.bg_failed", tender_id=tender.id, brief_id=brief_id
            )


@router.post(
    "/{tender_id}/generate-brief",
    status_code=202,
    response_model=TenderBriefStarted,
)
def start_generate_brief(
    tender_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> TenderBriefStarted:
    tender = db.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(status_code=404, detail="tender not found")
    now = datetime.now(UTC)
    placeholder = TenderBrief(
        tender_id=tender_id,
        status="generating",
        created_at=now,
        updated_at=now,
    )
    db.add(placeholder)
    db.commit()
    db.refresh(placeholder)
    # Bare-name reference so tests can monkeypatch.
    background_tasks.add_task(
        schedule_generate_brief, placeholder.id, tender_id
    )
    return TenderBriefStarted(
        id=placeholder.id,
        tender_id=placeholder.tender_id,
        status=placeholder.status,
        created_at=placeholder.created_at,
    )


@router.get(
    "/{tender_id}/brief",
    response_model=TenderBriefRead,
)
def get_latest_brief(
    tender_id: int, db: Session = Depends(get_db)
) -> TenderBriefRead:
    tender = db.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(status_code=404, detail="tender not found")
    latest = db.execute(
        select(TenderBrief)
        .where(TenderBrief.tender_id == tender_id)
        .order_by(TenderBrief.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is None:
        raise HTTPException(status_code=404, detail="no brief generated yet")
    return latest
