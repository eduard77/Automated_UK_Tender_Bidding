"""Pydantic schemas for the /portals and /portal-blocklist endpoints."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LoginType(StrEnum):
    none = "none"
    email_only = "email_only"
    username_password = "username_password"
    two_fa = "2fa"
    oauth = "oauth"
    unknown = "unknown"


class AdapterStatus(StrEnum):
    not_started = "not_started"
    stub = "stub"
    read_only = "read_only"
    full = "full"
    deprecated = "deprecated"


class Priority(StrEnum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"


class ExtractedFrom(StrEnum):
    description = "description"
    additional_information = "additional_information"
    documents = "documents"
    contact = "contact"
    parties = "parties"


class SightingType(StrEnum):
    tender_link = "tender_link"
    document_link = "document_link"
    contact_email = "contact_email"
    reference_text = "reference_text"


class PortalUrlSightingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    portal_id: int | None = None
    tender_id: int
    url: str
    extracted_from: ExtractedFrom
    sighting_type: SightingType
    extracted_at: datetime
    # Optional convenience for the detail view: caller can attach the tender
    # title without needing a second round-trip from the dashboard.
    tender_title: str | None = None


class PortalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    domain: str
    display_name: str
    url_patterns: list[str] = Field(default_factory=list)
    login_type: LoginType
    adapter_status: AdapterStatus
    adapter_module: str | None = None
    priority: Priority
    tender_count: int
    first_seen_at: datetime
    last_seen_at: datetime
    classification_data: dict[str, Any] | None = None
    notes: str | None = None
    created_at: datetime
    updated_at: datetime


class PortalDetailRead(PortalRead):
    """Single-portal response with the recent sightings inlined."""

    recent_sightings: list[PortalUrlSightingRead] = Field(default_factory=list)


class PortalUpdate(BaseModel):
    """PATCH body. Every field optional; only present fields are applied."""

    display_name: str | None = None
    login_type: LoginType | None = None
    priority: Priority | None = None
    adapter_status: AdapterStatus | None = None
    adapter_module: str | None = None
    notes: str | None = None


class PortalBlocklistEntryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    domain: str
    reason: str | None = None
    added_at: datetime
    added_by: str


class PortalBlocklistEntryCreate(BaseModel):
    domain: str
    reason: str | None = None


class ClassifyResponse(BaseModel):
    """Returned by POST /portals/{id}/classify."""

    portal_id: int
    status: str
    confidence: float | None = None
    queued: bool = False
