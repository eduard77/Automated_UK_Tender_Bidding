"""Platform registry HTTP surface."""
from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tender_agent.api.schemas.platforms import (
    PlatformPortalRow,
    PortalPlatformDetail,
    PortalPlatformListItem,
    PortalPlatformUpdate,
)
from tender_agent.db import get_db
from tender_agent.models import Portal, PortalPlatform, PortalUrlSighting

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/platforms", tags=["platforms"])


def _aggregates(db: Session) -> dict[int, tuple[int, int]]:
    """platform_id -> (total_tender_count, buyer_instance_count)."""
    rows = db.execute(
        select(
            Portal.platform_id,
            func.coalesce(func.sum(Portal.tender_count), 0),
            func.count(Portal.id),
        )
        .where(Portal.platform_id.is_not(None))
        .group_by(Portal.platform_id)
    ).all()
    return {row[0]: (int(row[1]), int(row[2])) for row in rows}


@router.get("", response_model=list[PortalPlatformListItem])
def list_platforms(db: Session = Depends(get_db)) -> list[PortalPlatformListItem]:
    platforms = list(
        db.execute(select(PortalPlatform).order_by(PortalPlatform.id)).scalars().all()
    )
    agg = _aggregates(db)
    out: list[PortalPlatformListItem] = []
    for p in platforms:
        total, count = agg.get(p.id, (0, 0))
        item = PortalPlatformListItem.model_validate(p)
        item.total_tender_count = total
        item.buyer_instance_count = count
        out.append(item)
    # Most impactful platforms first.
    out.sort(key=lambda x: x.total_tender_count, reverse=True)
    return out


@router.get("/{slug}", response_model=PortalPlatformDetail)
def get_platform(slug: str, db: Session = Depends(get_db)) -> PortalPlatformDetail:
    platform = db.execute(
        select(PortalPlatform).where(PortalPlatform.slug == slug)
    ).scalar_one_or_none()
    if platform is None:
        raise HTTPException(status_code=404, detail="platform not found")

    portals = list(
        db.execute(
            select(Portal)
            .where(Portal.platform_id == platform.id)
            .order_by(Portal.tender_count.desc())
        ).scalars().all()
    )
    total = sum(p.tender_count or 0 for p in portals)

    sample_urls: list[str] = []
    if portals:
        portal_ids = [p.id for p in portals]
        sample_urls = [
            row[0]
            for row in db.execute(
                select(PortalUrlSighting.url)
                .where(PortalUrlSighting.portal_id.in_(portal_ids))
                .where(
                    PortalUrlSighting.sighting_type.in_(
                        ("tender_link", "document_link")
                    )
                )
                .order_by(PortalUrlSighting.extracted_at.desc())
                .limit(3)
            ).all()
        ]

    detail = PortalPlatformDetail.model_validate(platform)
    detail.total_tender_count = total
    detail.buyer_instance_count = len(portals)
    detail.portals = [PlatformPortalRow.model_validate(p) for p in portals]
    detail.sample_tender_urls = sample_urls
    return detail


@router.patch("/{slug}", response_model=PortalPlatformListItem)
def update_platform(
    slug: str,
    body: PortalPlatformUpdate,
    db: Session = Depends(get_db),
) -> PortalPlatformListItem:
    platform = db.execute(
        select(PortalPlatform).where(PortalPlatform.slug == slug)
    ).scalar_one_or_none()
    if platform is None:
        raise HTTPException(status_code=404, detail="platform not found")
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        if hasattr(v, "value"):
            v = v.value
        setattr(platform, k, v)
    platform.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(platform)
    total, count = _aggregates(db).get(platform.id, (0, 0))
    item = PortalPlatformListItem.model_validate(platform)
    item.total_tender_count = total
    item.buyer_instance_count = count
    return item
