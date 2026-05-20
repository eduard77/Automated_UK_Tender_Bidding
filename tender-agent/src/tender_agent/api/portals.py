"""Portal registry + blocklist HTTP surface."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from tender_agent.api.schemas.portals import (
    AdapterStatus,
    ClassifyResponse,
    LoginType,
    PortalBlocklistEntryCreate,
    PortalBlocklistEntryRead,
    PortalDetailRead,
    PortalRead,
    PortalUpdate,
    PortalUrlSightingRead,
    Priority,
)
from tender_agent.db import SessionLocal, get_db
from tender_agent.models import (
    Portal,
    PortalBlocklistDomain,
    PortalUrlSighting,
    Tender,
)
from tender_agent.services.portal_classifier import (
    classify_portal,
    schedule_classification,
)

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["portals"])

PORTALS = APIRouter(prefix="/portals", tags=["portals"])
BLOCKLIST = APIRouter(prefix="/portal-blocklist", tags=["portals"])


# --- /portals ------------------------------------------------------------


def _portal_sort(adapter_status: str | None) -> Any:
    # Default sort uses a CASE expression for priority ordering, then
    # tender_count desc — mirroring "show me the most impactful portals first".
    from sqlalchemy import case

    return (
        case(
            (Portal.priority == "critical", 0),
            (Portal.priority == "high", 1),
            (Portal.priority == "medium", 2),
            (Portal.priority == "low", 3),
            else_=4,
        ),
        Portal.tender_count.desc(),
    )


@PORTALS.get("", response_model=list[PortalRead])
def list_portals(
    db: Session = Depends(get_db),
    adapter_status: AdapterStatus | None = Query(None),
    priority: Priority | None = Query(None),
    search: str | None = Query(None),
    has_login_type: LoginType | None = Query(None),
    platform_id: int | None = Query(None),
    include_email_domains: bool = Query(
        False, description="Include is_email_domain portals (hidden by default)"
    ),
    sort: str | None = Query(
        None,
        description="One of: priority (default), tender_count, last_seen, first_seen",
    ),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[PortalRead]:
    stmt = select(Portal)
    if adapter_status:
        stmt = stmt.where(Portal.adapter_status == adapter_status.value)
    if priority:
        stmt = stmt.where(Portal.priority == priority.value)
    if has_login_type:
        stmt = stmt.where(Portal.login_type == has_login_type.value)
    if platform_id is not None:
        stmt = stmt.where(Portal.platform_id == platform_id)
    if not include_email_domains:
        stmt = stmt.where(Portal.is_email_domain.is_(False))
    if search:
        pattern = f"%{search.lower()}%"
        stmt = stmt.where(
            or_(
                Portal.display_name.ilike(pattern),
                Portal.domain.ilike(pattern),
            )
        )

    if sort == "tender_count":
        stmt = stmt.order_by(Portal.tender_count.desc())
    elif sort == "last_seen":
        stmt = stmt.order_by(Portal.last_seen_at.desc())
    elif sort == "first_seen":
        stmt = stmt.order_by(Portal.first_seen_at.asc())
    else:
        stmt = stmt.order_by(*_portal_sort(adapter_status.value if adapter_status else None))

    stmt = stmt.offset(offset).limit(limit)
    return list(db.execute(stmt).scalars().all())


@PORTALS.get("/{portal_id}", response_model=PortalDetailRead)
def get_portal(portal_id: int, db: Session = Depends(get_db)) -> PortalDetailRead:
    portal = db.get(Portal, portal_id)
    if portal is None:
        raise HTTPException(status_code=404, detail="portal not found")
    sightings = list(
        db.execute(
            select(PortalUrlSighting, Tender.title)
            .join(Tender, Tender.id == PortalUrlSighting.tender_id, isouter=True)
            .where(PortalUrlSighting.portal_id == portal_id)
            .order_by(PortalUrlSighting.extracted_at.desc())
            .limit(20)
        ).all()
    )
    recent: list[PortalUrlSightingRead] = []
    for sighting, title in sightings:
        item = PortalUrlSightingRead.model_validate(sighting)
        item.tender_title = title
        recent.append(item)
    payload = PortalDetailRead.model_validate(portal).model_dump()
    payload["recent_sightings"] = [s.model_dump() for s in recent]
    return PortalDetailRead.model_validate(payload)


@PORTALS.patch("/{portal_id}", response_model=PortalRead)
def update_portal(
    portal_id: int,
    body: PortalUpdate,
    db: Session = Depends(get_db),
) -> PortalRead:
    portal = db.get(Portal, portal_id)
    if portal is None:
        raise HTTPException(status_code=404, detail="portal not found")
    data = body.model_dump(exclude_unset=True)
    # Pydantic gives us enum instances; the DB columns store the raw strings.
    for k, v in list(data.items()):
        if hasattr(v, "value"):
            data[k] = v.value
    for k, v in data.items():
        setattr(portal, k, v)
    portal.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(portal)
    return portal


@PORTALS.delete("/{portal_id}", response_model=PortalRead)
def deprecate_portal(portal_id: int, db: Session = Depends(get_db)) -> PortalRead:
    """Soft-delete by setting adapter_status='deprecated'. Hard delete would
    drop the audit trail of every sighting we've recorded — disallowed."""
    portal = db.get(Portal, portal_id)
    if portal is None:
        raise HTTPException(status_code=404, detail="portal not found")
    portal.adapter_status = "deprecated"
    portal.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(portal)
    return portal


@PORTALS.post("/{portal_id}/classify", response_model=ClassifyResponse)
def reclassify_portal(
    portal_id: int,
    background_tasks: BackgroundTasks,
    sync: bool = Query(False, description="Run classification inline (debugging)"),
    db: Session = Depends(get_db),
) -> ClassifyResponse:
    portal = db.get(Portal, portal_id)
    if portal is None:
        raise HTTPException(status_code=404, detail="portal not found")

    if sync:
        result = classify_portal(portal_id, db)
        return ClassifyResponse(
            portal_id=portal_id,
            status=result.status,
            confidence=result.confidence,
            queued=False,
        )

    background_tasks.add_task(schedule_classification, portal_id)
    return ClassifyResponse(
        portal_id=portal_id,
        status="queued",
        queued=True,
    )


@PORTALS.get("/{portal_id}/sightings", response_model=list[PortalUrlSightingRead])
def list_sightings(
    portal_id: int,
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[PortalUrlSightingRead]:
    if db.get(Portal, portal_id) is None:
        raise HTTPException(status_code=404, detail="portal not found")
    rows = db.execute(
        select(PortalUrlSighting, Tender.title)
        .join(Tender, Tender.id == PortalUrlSighting.tender_id, isouter=True)
        .where(PortalUrlSighting.portal_id == portal_id)
        .order_by(PortalUrlSighting.extracted_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    out: list[PortalUrlSightingRead] = []
    for sighting, title in rows:
        item = PortalUrlSightingRead.model_validate(sighting)
        item.tender_title = title
        out.append(item)
    return out


# --- /portal-blocklist ---------------------------------------------------


@BLOCKLIST.get("", response_model=list[PortalBlocklistEntryRead])
def list_blocklist(db: Session = Depends(get_db)) -> list[PortalBlocklistEntryRead]:
    return list(
        db.execute(
            select(PortalBlocklistDomain).order_by(PortalBlocklistDomain.domain)
        ).scalars().all()
    )


@BLOCKLIST.post("", response_model=PortalBlocklistEntryRead, status_code=201)
def add_blocklist_entry(
    body: PortalBlocklistEntryCreate,
    db: Session = Depends(get_db),
) -> PortalBlocklistEntryRead:
    domain = body.domain.strip().lower()
    if not domain:
        raise HTTPException(status_code=400, detail="domain required")
    existing = db.execute(
        select(PortalBlocklistDomain).where(PortalBlocklistDomain.domain == domain)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="domain already blocked")
    entry = PortalBlocklistDomain(domain=domain, reason=body.reason, added_by="user")
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


@BLOCKLIST.delete("/{entry_id}", status_code=204)
def remove_blocklist_entry(entry_id: int, db: Session = Depends(get_db)) -> None:
    entry = db.get(PortalBlocklistDomain, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="entry not found")
    db.delete(entry)
    db.commit()


# Aggregate router for main.py to mount.
router.include_router(PORTALS)
router.include_router(BLOCKLIST)


__all__ = ["router", "SessionLocal"]
