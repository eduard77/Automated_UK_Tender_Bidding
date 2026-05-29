"""Pydantic schemas for API I/O and internal exchange between adapters and services."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class TenderDocument(BaseModel):
    title: str | None = None
    url: str
    format: str | None = None


class NormalisedTender(BaseModel):
    """Canonical normalised tender shape produced by adapters/normaliser."""

    source_code: str
    source_ref: str
    source_url: str | None = None
    procurement_ref: str | None = None

    title: str
    description: str | None = None
    notice_type: str | None = None
    status: str | None = None

    buyer_name: str | None = None
    buyer_id: str | None = None
    buyer_country: str | None = None
    buyer_region: str | None = None

    cpv_codes: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)

    value_amount: Decimal | None = None
    value_currency: str | None = None
    value_min: Decimal | None = None
    value_max: Decimal | None = None

    published_at: datetime | None = None
    deadline_at: datetime | None = None
    contract_start: date | None = None
    contract_end: date | None = None

    documents: list[TenderDocument] = Field(default_factory=list)
    raw: dict = Field(default_factory=dict)


class TenderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_code: str
    source_ref: str
    source_url: str | None
    title: str
    description: str | None
    buyer_name: str | None
    notice_type: str | None
    status: str | None
    cpv_codes: list[str] | None
    value_amount: Decimal | None
    value_currency: str | None
    published_at: datetime | None
    deadline_at: datetime | None
    documents: list[dict] | None
    first_seen_at: datetime
    last_seen_at: datetime


class FilterProfileCreate(BaseModel):
    name: str
    enabled: bool = True
    cpv_codes: list[str] | None = None
    cpv_prefixes: list[str] | None = None
    keywords_any: list[str] | None = None
    keywords_all: list[str] | None = None
    keywords_none: list[str] | None = None
    buyer_names: list[str] | None = None
    regions: list[str] | None = None
    countries: list[str] | None = None
    notice_types: list[str] | None = None
    value_min: Decimal | None = None
    value_max: Decimal | None = None
    min_days_to_deadline: int | None = None


class FilterProfileUpdate(BaseModel):
    """Partial update — all fields optional. Omitted fields keep their stored value."""

    name: str | None = None
    enabled: bool | None = None
    cpv_codes: list[str] | None = None
    cpv_prefixes: list[str] | None = None
    keywords_any: list[str] | None = None
    keywords_all: list[str] | None = None
    keywords_none: list[str] | None = None
    buyer_names: list[str] | None = None
    regions: list[str] | None = None
    countries: list[str] | None = None
    notice_types: list[str] | None = None
    value_min: Decimal | None = None
    value_max: Decimal | None = None
    min_days_to_deadline: int | None = None


class FilterProfileRead(FilterProfileCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    match_count: int = 0


class TenderMatchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tender_id: int
    filter_profile_id: int
    score: Decimal
    matched_at: datetime
    notified_at: datetime | None


class TenderRequirementsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tender_id: int
    summary: str | None
    evaluation_criteria: list[dict] | None
    mandatory_requirements: list[dict] | None
    desired_requirements: list[dict] | None
    documents_required: list[dict] | None
    questions_to_answer: list[dict] | None
    risk_flags: list[str] | None
    estimated_effort_days: Decimal | None
    recommendation: str | None
    recommendation_reason: str | None
    model: str | None
    extracted_at: datetime


class TenderDocumentFileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tender_id: int
    url: str
    title: str | None
    format: str | None
    bytes: int | None
    sha256: str | None
    download_status: str
    error: str | None
    downloaded_at: datetime | None
    # Chunk 5: whether stored, reusable content exists for this file (joined
    # in by the API; the field is computed, never queried directly).
    content_stored: bool = False


class TenderDocumentContentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    document_file_id: int
    tender_id: int
    sha256: str
    char_count: int | None
    extraction_status: str
    extraction_detail: str | None
    doc_type: str | None
    extractor_version: str
    created_at: datetime
    updated_at: datetime


class TenderBriefRead(BaseModel):
    """A bid-brief generation record (chunk 5)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    tender_id: int
    status: str
    recommendation: str | None
    confidence: str | None
    headline: str | None
    brief_json: dict | None
    model: str | None
    documents_considered: list[dict] | None
    input_tokens: int | None
    output_tokens: int | None
    error_detail: str | None
    generated_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PushSubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscriptionCreate(BaseModel):
    """Mirrors the browser's PushSubscription.toJSON() plus an optional filter pin."""

    endpoint: str
    keys: PushSubscriptionKeys
    filter_profile_id: int | None = None


class PushUnsubscribe(BaseModel):
    endpoint: str


class PushSubscriptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    endpoint: str
    filter_profile_id: int | None
    user_agent: str | None
    created_at: datetime
    last_used_at: datetime | None


class VapidPublicKey(BaseModel):
    """Public VAPID key + subject. Public key only — private key never leaves the backend."""

    public_key: str
    subject: str
