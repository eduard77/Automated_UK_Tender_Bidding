"""Filter profile management + admin trigger endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tender_agent import scheduler
from tender_agent.db import get_db
from tender_agent.models import FilterMatch, FilterProfile
from tender_agent.schemas import (
    FilterProfileCreate,
    FilterProfileRead,
    FilterProfileUpdate,
)

router = APIRouter(tags=["filters"])


def _to_read(profile: FilterProfile, match_count: int) -> FilterProfileRead:
    return FilterProfileRead.model_validate(
        {**profile.__dict__, "match_count": match_count}
    )


def _load(db: Session, filter_id: int) -> FilterProfile:
    profile = db.get(FilterProfile, filter_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="filter not found")
    return profile


def _count_matches(db: Session, filter_id: int) -> int:
    return int(
        db.execute(
            select(func.count(FilterMatch.id)).where(
                FilterMatch.filter_profile_id == filter_id
            )
        ).scalar_one()
    )


@router.get("/filters", response_model=list[FilterProfileRead])
def list_filters(db: Session = Depends(get_db)) -> list[FilterProfileRead]:
    rows = db.execute(
        select(FilterProfile, func.count(FilterMatch.id))
        .outerjoin(FilterMatch, FilterMatch.filter_profile_id == FilterProfile.id)
        .group_by(FilterProfile.id)
        .order_by(FilterProfile.created_at.desc())
    ).all()
    return [_to_read(profile, count) for profile, count in rows]


@router.post("/filters", response_model=FilterProfileRead, status_code=201)
def create_filter(
    payload: FilterProfileCreate, db: Session = Depends(get_db)
) -> FilterProfileRead:
    profile = FilterProfile(**payload.model_dump())
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return _to_read(profile, 0)


@router.put("/filters/{filter_id}", response_model=FilterProfileRead)
def replace_filter(
    filter_id: int,
    payload: FilterProfileCreate,
    db: Session = Depends(get_db),
) -> FilterProfileRead:
    profile = _load(db, filter_id)
    for key, value in payload.model_dump().items():
        setattr(profile, key, value)
    db.commit()
    db.refresh(profile)
    return _to_read(profile, _count_matches(db, filter_id))


@router.patch("/filters/{filter_id}", response_model=FilterProfileRead)
def update_filter(
    filter_id: int,
    payload: FilterProfileUpdate,
    db: Session = Depends(get_db),
) -> FilterProfileRead:
    profile = _load(db, filter_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(profile, key, value)
    db.commit()
    db.refresh(profile)
    return _to_read(profile, _count_matches(db, filter_id))


@router.delete("/filters/{filter_id}", status_code=204)
def delete_filter(filter_id: int, db: Session = Depends(get_db)) -> None:
    profile = _load(db, filter_id)
    db.delete(profile)
    db.commit()


@router.post("/admin/poll-now", status_code=202)
async def poll_now() -> dict:
    """Trigger an immediate poll cycle across all enabled sources.

    Single-flight: when a cycle is already running the trigger is SKIPPED
    (status "skipped_already_running") instead of starting a concurrent
    duplicate — concurrent cycles double-polled sources and contributed to
    the 2026-06-12 discovery outage."""
    started = await scheduler.trigger_now()
    return {"status": "queued" if started else "skipped_already_running"}
