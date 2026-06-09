"""Pydantic I/O models for the email integration API."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ProviderStatus(BaseModel):
    provider: str
    # configured = the operator's one-time OAuth app setup is done.
    configured: bool
    # implemented = the provider is built (Yahoo is deferred -> False).
    implemented: bool


class ConnectResponse(BaseModel):
    provider: str
    authorization_url: str


class MailboxConnectionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider: str
    email_address: str | None
    status: str
    last_polled_at: datetime | None
    last_error: str | None
    created_at: datetime


class MailboxMessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider_message_id: str
    tender_id: int | None
    matched_ref: str | None
    subject: str | None
    sender: str | None
    received_at: datetime | None
    body_excerpt: str | None
    attachment_count: int
    links: list | None
    draft_reply: str | None
    draft_status: str | None
    created_at: datetime


class PollNowResponse(BaseModel):
    mailbox_id: int
    listed: int
    filed: int
    skipped_existing: int
    no_match: int
    errors: int
