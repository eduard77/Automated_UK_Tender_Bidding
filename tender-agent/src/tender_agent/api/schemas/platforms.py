"""Pydantic schemas for the /platforms endpoints."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from tender_agent.api.schemas.portals import AdapterStatus, LoginType


class PlatformKind:
    transactional = "transactional"
    special = "special"


class PortalPlatformRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    vendor: str
    display_name: str
    domain_patterns: list[str] = Field(default_factory=list)
    kind: str
    adapter_status: AdapterStatus
    adapter_module: str | None = None
    login_type: LoginType
    notes: str | None = None
    created_at: datetime
    updated_at: datetime


class PlatformPortalRow(BaseModel):
    """A linked buyer-instance portal, summarised for the platform detail page."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    domain: str
    display_name: str
    tender_count: int
    adapter_status: AdapterStatus


class PortalPlatformListItem(PortalPlatformRead):
    """List-view row: platform + aggregate counts across linked portals."""

    total_tender_count: int = 0
    buyer_instance_count: int = 0


class PortalPlatformDetail(PortalPlatformListItem):
    portals: list[PlatformPortalRow] = Field(default_factory=list)
    sample_tender_urls: list[str] = Field(default_factory=list)


class PortalPlatformUpdate(BaseModel):
    """Admin override of a platform row."""

    adapter_status: AdapterStatus | None = None
    adapter_module: str | None = None
    login_type: LoginType | None = None
    notes: str | None = None
