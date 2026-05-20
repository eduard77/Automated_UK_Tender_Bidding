"""Pydantic schemas for the /credentials endpoints.

Passwords are never returned. Read models carry metadata only.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CredentialMetadataRead(BaseModel):
    portal_id: int
    platform_slug: str | None = None
    valid: bool
    last_used_at: str | None = None
    last_validated_at: str | None = None
    # Convenience enrichment from the main DB (the credentials store itself
    # doesn't know the portal's display name / domain).
    portal_domain: str | None = None
    portal_display_name: str | None = None


class CredentialCreate(BaseModel):
    portal_id: int
    username: str | None = None
    password: str | None = None
    email: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class CredentialValidateResponse(BaseModel):
    portal_id: int
    valid: bool
    status: str
    detail: str | None = None
