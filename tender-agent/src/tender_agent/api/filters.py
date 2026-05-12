"""Filter profile management + admin trigger endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent import scheduler
from tender_agent.db import get_db
from tender_agent.models import FilterProfile
from tender_agent.schemas import FilterProfileCreate, FilterProfileRead

router = APIRouter(tags=["filters"])


@router.get("/filters", response_model=list[FilterProfileRead])
def list_filters(db: Session = Depends(get_db)) -> list[FilterProfileRead]:
    return list(db.execute(select(FilterProfile)).scalars().all())


@router.post("/filters", response_model=FilterProfileRead, status_code=201)
def create_filter(payload: FilterProfileCreate, db: Session = Depends(get_db)) -> FilterProfileRead:
    profile = FilterProfile(**payload.model_dump())
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


@router.delete("/filters/{filter_id}", status_code=204)
def delete_filter(filter_id: int, db: Session = Depends(get_db)) -> None:
    profile = db.get(FilterProfile, filter_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="filter not found")
    db.delete(profile)
    db.commit()


@router.post("/admin/poll-now", status_code=202)
async def poll_now() -> dict:
    """Trigger an immediate poll cycle across all enabled sources."""
    await scheduler.trigger_now()
    return {"status": "queued"}
