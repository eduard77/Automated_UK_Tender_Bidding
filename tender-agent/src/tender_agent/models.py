"""ORM models. Canonical schema for tenders normalised across sources."""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Source(Base):
    """A tender publication source (FTS, Contracts Finder, etc.)."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    runs: Mapped[list[PollRun]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )


class Tender(Base):
    """Canonical normalised tender record."""

    __tablename__ = "tenders"
    __table_args__ = (
        UniqueConstraint("source_code", "source_ref", name="uq_tender_source_ref"),
        Index("ix_tenders_published_at", "published_at"),
        Index("ix_tenders_deadline_at", "deadline_at"),
        Index("ix_tenders_value_amount", "value_amount"),
        Index("ix_tenders_buyer_name", "buyer_name"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # Source provenance
    source_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(1024))

    # Cross-source dedupe
    procurement_ref: Mapped[str | None] = mapped_column(String(256), index=True)
    duplicate_of_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("tenders.id"), index=True
    )

    # Core
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    notice_type: Mapped[str | None] = mapped_column(String(64))  # e.g. contract, prior, award
    status: Mapped[str | None] = mapped_column(String(32))  # active, closed, awarded, cancelled

    # Buyer
    buyer_name: Mapped[str | None] = mapped_column(String(512))
    buyer_id: Mapped[str | None] = mapped_column(String(128))
    buyer_country: Mapped[str | None] = mapped_column(String(64))
    buyer_region: Mapped[str | None] = mapped_column(String(128))
    # Canonical UK region (chunk 8) extracted from raw OCDS delivery-address data
    # (region field -> postcode -> country). buyer_region is kept untouched as
    # the raw source value; this is the normalised, filterable region.
    region: Mapped[str | None] = mapped_column(String(64), index=True)

    # Classification
    cpv_codes: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    keywords: Mapped[list[str] | None] = mapped_column(ARRAY(String))

    # Value
    value_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    value_currency: Mapped[str | None] = mapped_column(String(8))
    value_min: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    value_max: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))

    # Dates
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    contract_start: Mapped[datetime | None] = mapped_column(Date)
    contract_end: Mapped[datetime | None] = mapped_column(Date)

    # Documents (URLs to ITT, spec, etc.) — list of {title, url, format}
    documents: Mapped[list[dict] | None] = mapped_column(JSON)

    # Raw payload for debugging / re-extraction
    raw: Mapped[dict | None] = mapped_column(JSON)

    # Audit
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    content_hash: Mapped[str | None] = mapped_column(String(64))

    matches: Mapped[list[FilterMatch]] = relationship(
        back_populates="tender", cascade="all, delete-orphan"
    )
    document_files: Mapped[list[TenderDocumentFile]] = relationship(
        back_populates="tender", cascade="all, delete-orphan"
    )
    requirements: Mapped[TenderRequirements | None] = relationship(
        back_populates="tender", uselist=False, cascade="all, delete-orphan"
    )


class TenderDocumentFile(Base):
    """A downloaded copy of a document attached to a tender."""

    __tablename__ = "tender_document_files"
    __table_args__ = (
        UniqueConstraint("tender_id", "url", name="uq_doc_tender_url"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tender_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    title: Mapped[str | None] = mapped_column(String(512))
    format: Mapped[str | None] = mapped_column(String(32))
    storage_key: Mapped[str | None] = mapped_column(String(512))
    storage_backend: Mapped[str] = mapped_column(String(16), default="local", nullable=False)
    bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    text_extracted: Mapped[str | None] = mapped_column(Text)
    download_status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    tender: Mapped[Tender] = relationship(back_populates="document_files")
    content_rows: Mapped[list[TenderDocumentContent]] = relationship(
        back_populates="document_file", cascade="all, delete-orphan"
    )


class TenderDocumentContent(Base):
    """Extracted text content from a tender document, keyed by (sha256, extractor_version).

    Same content + same extractor = ONE row, reused across every tender the
    document appears on. The bytes on disk are a cache; this table is the
    durable, queryable, cloud-portable home for document content. Chunk 5
    Part A.
    """

    __tablename__ = "tender_document_content"
    __table_args__ = (
        UniqueConstraint("sha256", "extractor_version", name="uq_tdc_sha_version"),
        Index("ix_tender_document_content_tender_id", "tender_id"),
        Index("ix_tender_document_content_document_file_id", "document_file_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_file_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tender_document_files.id", ondelete="CASCADE"),
        nullable=False,
    )
    tender_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tenders.id", ondelete="CASCADE"),
        nullable=False,
    )
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    extracted_text: Mapped[str | None] = mapped_column(Text)
    char_count: Mapped[int | None] = mapped_column(Integer)
    extraction_status: Mapped[str] = mapped_column(Text, nullable=False)
    extraction_detail: Mapped[str | None] = mapped_column(Text)
    doc_type: Mapped[str | None] = mapped_column(Text)
    extractor_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    document_file: Mapped[TenderDocumentFile] = relationship(
        back_populates="content_rows"
    )


class TenderRequirements(Base):
    """Structured requirements extracted from a tender's documents by Claude."""

    __tablename__ = "tender_requirements"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tender_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tenders.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    summary: Mapped[str | None] = mapped_column(Text)
    evaluation_criteria: Mapped[list[dict] | None] = mapped_column(JSON)
    mandatory_requirements: Mapped[list[dict] | None] = mapped_column(JSON)
    desired_requirements: Mapped[list[dict] | None] = mapped_column(JSON)
    documents_required: Mapped[list[dict] | None] = mapped_column(JSON)
    questions_to_answer: Mapped[list[dict] | None] = mapped_column(JSON)
    risk_flags: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    estimated_effort_days: Mapped[float | None] = mapped_column(Numeric(5, 2))
    recommendation: Mapped[str | None] = mapped_column(String(32))  # pursue/decline/review
    recommendation_reason: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(64))
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    raw_response: Mapped[dict | None] = mapped_column(JSON)

    tender: Mapped[Tender] = relationship(back_populates="requirements")


class TenderBrief(Base):
    """A bid-brief generation record (chunk 5).

    Multiple rows per tender: each generate-brief request inserts one. The
    dashboard reads the latest by created_at. Status transitions:
        generating → complete | failed.
    """

    __tablename__ = "tender_briefs"
    __table_args__ = (
        Index(
            "ix_tender_briefs_tender_created",
            "tender_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tender_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tenders.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="generating")
    recommendation: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[str | None] = mapped_column(Text)
    headline: Mapped[str | None] = mapped_column(Text)
    brief_json: Mapped[dict | None] = mapped_column(JSONB)
    model: Mapped[str | None] = mapped_column(Text)
    documents_considered: Mapped[list[dict] | None] = mapped_column(JSONB)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    error_detail: Mapped[str | None] = mapped_column(Text)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class RegionLookup(Base):
    """Cache of resolved region keys (chunk 8).

    Each distinct unresolved location key (a postcode area, a country name, or a
    free-text location handed to the AI fallback) is resolved once and reused.
    ``method`` records how it was resolved so a backfill can report the mix and
    so the rare AI calls are never repeated for the same key."""

    __tablename__ = "region_lookup"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    raw_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    canonical_region: Mapped[str] = mapped_column(String(64), nullable=False)
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class FilterProfile(Base):
    """A user-defined filter for incoming tenders."""

    __tablename__ = "filter_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Match criteria — all optional; AND between fields, OR within a field
    cpv_codes: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    cpv_prefixes: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    keywords_any: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    keywords_all: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    keywords_none: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    buyer_names: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    regions: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    countries: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    notice_types: Mapped[list[str] | None] = mapped_column(ARRAY(String))

    value_min: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    value_max: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    min_days_to_deadline: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    matches: Mapped[list[FilterMatch]] = relationship(
        back_populates="filter_profile", cascade="all, delete-orphan"
    )


class FilterMatch(Base):
    """Records that a tender matched a filter profile, for alerting."""

    __tablename__ = "filter_matches"
    __table_args__ = (
        UniqueConstraint("tender_id", "filter_profile_id", name="uq_match_tender_filter"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tender_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filter_profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("filter_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    score: Mapped[float] = mapped_column(Numeric(5, 4), default=1.0, nullable=False)
    matched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    tender: Mapped[Tender] = relationship(back_populates="matches")
    filter_profile: Mapped[FilterProfile] = relationship(back_populates="matches")


class PollRun(Base):
    """Audit record for each poll attempt against a source."""

    __tablename__ = "poll_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    fetched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    new_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)

    source: Mapped[Source] = relationship(back_populates="runs")


class PushSubscription(Base):
    """Browser Web Push subscription, OWNED by an Account.

    `account_id` ties a subscription (and its device) to one user. Every
    notification path dispatches only to a specific account's subscriptions, so
    one user's notifications — especially the email-derived ones that carry
    private inbox content — never reach another user's devices.

    `account_id IS NULL` marks a LEGACY row created before ownership existed.
    Such rows are unowned and are EXCLUDED from every per-user dispatch (they
    are never blasted as a catch-all). They simply stop receiving until the
    device re-subscribes (now authenticated), or an operator backfills them.

    `filter_profile_id IS NULL` means "every match for my account"; a specific
    profile means "only matches against that profile" — now always AND-ed with
    the owning account.
    """

    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("accounts.id", ondelete="CASCADE"),
        index=True,
    )
    filter_profile_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("filter_profiles.id", ondelete="CASCADE"),
        index=True,
    )
    endpoint: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    p256dh: Mapped[str] = mapped_column(Text, nullable=False)
    auth: Mapped[str] = mapped_column(Text, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VaultDocument(Base):
    """A document in the document vault. The vault stores everything the agent
    might cite back to a buyer (insurance certs, ISO certs, policies, case
    studies, accounts, capability statements, etc.). Per PROJECT.md §5.4
    documents are versioned and never deleted — supersede in place.
    """

    __tablename__ = "vault_documents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    org_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1, index=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    subcategory: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    owner_email: Mapped[str | None] = mapped_column(String(256))
    confidentiality: Mapped[str] = mapped_column(
        String(16), nullable=False, default="internal"
    )

    # Pointer to the current (non-superseded) version. Null while uploading
    # the first version. Set after the first version row is inserted.
    current_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "vault_document_versions.id", ondelete="SET NULL", use_alter=True
        ),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    # Soft delete — bids reference specific version IDs, so we never hard-delete.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    versions: Mapped[list[VaultDocumentVersion]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        foreign_keys="VaultDocumentVersion.document_id",
        order_by="VaultDocumentVersion.version",
    )
    current_version: Mapped[VaultDocumentVersion | None] = relationship(
        foreign_keys=[current_version_id],
        post_update=True,
    )


class VaultDocumentVersion(Base):
    """One version of a vault document. Claims are the machine-readable summary
    of what the document proves (insurance cover amount, ISO scope, policy
    signatory, case study client + value, etc.) — see services/vault/claims_schemas.py
    for the per-doc-type shape.
    """

    __tablename__ = "vault_document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version", name="uq_vault_version"),
        Index("ix_vault_versions_expiry", "expiry_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("vault_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    # Blob metadata
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)

    # Issuance / expiry (extracted from claims; mirrored as columns here for
    # cheap WHERE filtering by the matcher and the /vault/expiring endpoint).
    expiry_date: Mapped[date | None] = mapped_column(Date)
    issuing_body: Mapped[str | None] = mapped_column(String(256))
    issued_date: Mapped[date | None] = mapped_column(Date)
    last_reviewed_date: Mapped[date | None] = mapped_column(Date)

    superseded_by_version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("vault_document_versions.id"), index=True
    )

    # Machine-readable claims (see services/vault/claims_schemas.py).
    claims: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    claims_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    text_extracted: Mapped[str | None] = mapped_column(Text)
    # pgvector 1536-dim. Matches OpenAI text-embedding-3-small (default
    # production provider per docs/vault.md). Null when no embedder is wired.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536))

    uploaded_by: Mapped[str | None] = mapped_column(String(256))
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    document: Mapped[VaultDocument] = relationship(
        back_populates="versions", foreign_keys=[document_id]
    )


class PortalPlatform(Base):
    """An e-tendering *product* (Delta eSourcing, ProContract, JAGGAER, ...).

    One platform owns many buyer-instance portals. Membership is computed from
    `domain_patterns` (a list of Python regexes matched against
    `Portal.domain`). Adapters are built per-platform, not per-portal: one
    Delta adapter serves every Delta-hosted buyer.
    """

    __tablename__ = "portal_platforms"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    vendor: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    domain_patterns: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="transactional")
    adapter_status: Mapped[str] = mapped_column(
        Text, nullable=False, default="not_started"
    )
    adapter_module: Mapped[str | None] = mapped_column(Text)
    login_type: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    portals: Mapped[list[Portal]] = relationship(back_populates="platform")


class Portal(Base):
    """A procurement portal we've discovered — typically a buyer-hosted or
    aggregator-hosted site that publishes ITT documents and gates them behind
    login / registration.

    Created lazily the first time a URL from this domain appears in any
    ingested tender. Classification is performed asynchronously by Claude;
    until then the row is a placeholder with sensible defaults.
    """

    __tablename__ = "portals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    domain: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    url_patterns: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    login_type: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    adapter_status: Mapped[str] = mapped_column(
        Text, nullable=False, default="not_started"
    )
    adapter_module: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(Text, nullable=False, default="medium")
    tender_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    classification_data: Mapped[dict | None] = mapped_column(JSONB)
    notes: Mapped[str | None] = mapped_column(Text)
    # Platform-first layer (chunk 2). Null when no known platform owns this
    # domain (buyer-direct gov.uk sites, NHS email domains, etc.).
    platform_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("portal_platforms.id", ondelete="SET NULL"), index=True
    )
    # True when every sighting for this portal is a contact email (e.g.
    # nhs.net): not a real portal, hidden from the registry by default.
    is_email_domain: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    sightings: Mapped[list[PortalUrlSighting]] = relationship(
        back_populates="portal", cascade="all, delete-orphan"
    )
    platform: Mapped[PortalPlatform | None] = relationship(
        back_populates="portals"
    )


class PortalUrlSighting(Base):
    """One observed URL on a tender, linked to a Portal once the domain is
    associated. Multiple sightings per (portal, tender) are expected — e.g.
    the description has a link, and three document attachments live on the
    same domain.
    """

    __tablename__ = "portal_url_sightings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    portal_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("portals.id", ondelete="CASCADE"),
        index=True,
    )
    tender_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tenders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    extracted_from: Mapped[str] = mapped_column(Text, nullable=False)
    sighting_type: Mapped[str] = mapped_column(Text, nullable=False)
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    portal: Mapped[Portal | None] = relationship(back_populates="sightings")
    tender: Mapped[Tender] = relationship()


class PortalBlocklistDomain(Base):
    """Domains we never cluster as portals. Seeded with our own source
    domains (Contracts Finder, etc.), social media, and the gov.uk apex;
    user-managed via the dashboard."""

    __tablename__ = "portal_blocklist_domains"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    domain: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    reason: Mapped[str | None] = mapped_column(Text)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    added_by: Mapped[str] = mapped_column(Text, nullable=False, default="system")


class FetchTask(Base):
    """One 'fetch documents' run for a tender. Created when the user clicks
    Fetch documents; updated as the orchestrator progresses. Durable record
    that replaces the chunk-2 in-memory stub."""

    __tablename__ = "fetch_tasks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    tender_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tenders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    adapter: Mapped[str | None] = mapped_column(Text)
    platform_slug: Mapped[str | None] = mapped_column(Text)
    files_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missing_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict | None] = mapped_column(JSONB)
    # Login-via-human flow (chunk 4): set while a task waits for the user to
    # log in to a portal in the visible browser window.
    login_url: Mapped[str | None] = mapped_column(Text)
    bridge_session_slug: Mapped[str | None] = mapped_column(Text)
    waiting_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# Phase 4 chunk 6 — accounts, plans, brief entitlements, submission payments.
# Search stays anonymous; these tables back the optional logged-in tiers and
# the per-tender unlock model documented in api/accounts.py & services/billing.
# ---------------------------------------------------------------------------


class Account(Base):
    """A registered user. Email + password (hashed); free alert subscribers
    have plan='free' and never touch Stripe."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # plan = 'free' | 'payg' | 'plan_100' | 'plan_unlimited'.
    # 'payg' just means "has bought one or more single briefs"; entitlement
    # rows are the source of truth for what they can SEE — plan only controls
    # the monthly allowance.
    plan: Mapped[str] = mapped_column(String(24), nullable=False, default="free")
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    brief_generations_this_period: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    period_anchor: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    entitlements: Mapped[list[BriefEntitlement]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    sessions: Mapped[list[AccountSession]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class AccountSession(Base):
    """Server-side session token table. We don't sign cookies — we store the
    opaque token here and look it up. Tokens are random URL-safe strings."""

    __tablename__ = "account_sessions"
    __table_args__ = (
        Index("ix_account_sessions_account_id", "account_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    token: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    account: Mapped[Account] = relationship(back_populates="sessions")


class BriefEntitlement(Base):
    """One row = `account` may view the FULL brief + FULL documents for
    `tender`. PAYG grants exactly one entitlement; plans (plan_100,
    plan_unlimited) create one on each first-time brief generation.

    Plans do NOT auto-grant access to every tender — they enable on-demand
    generation. The entitlement is what unlocks viewing later, and survives
    a plan downgrade so already-bought reads remain visible (we never delete
    audit history)."""

    __tablename__ = "brief_entitlements"
    __table_args__ = (
        UniqueConstraint("account_id", "tender_id", name="uq_brief_entitlement"),
        Index("ix_brief_entitlements_account_id", "account_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    tender_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tenders.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 'payg' | 'plan' | 'dev'  — dev is only writable when not in production.
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    account: Mapped[Account] = relationship(back_populates="entitlements")


class SubmissionPackagePayment(Base):
    """Per-tender, per-account record of a paid (or pending) submission-package
    fee. The fee is dynamic (0.5% of contract_value clamped £100–£300) and
    charged upfront via a one-off Stripe Checkout Session. Status flips to
    'paid' on `checkout.session.completed`. NEVER on subscription."""

    __tablename__ = "submission_package_payments"
    __table_args__ = (
        Index(
            "ix_subpkg_account_tender",
            "account_id",
            "tender_id",
        ),
        Index("ix_subpkg_checkout_session", "stripe_checkout_session_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    tender_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tenders.id", ondelete="CASCADE"),
        nullable=False,
    )
    amount_pence: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="GBP")
    # 'pending' | 'paid' | 'cancelled'
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    stripe_checkout_session_id: Mapped[str | None] = mapped_column(String(128))
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# Submission-package generator (drafting spine).
# Drafts are NEVER auto-submitted — these tables exist purely to record what
# the drafting agent produced for a human reviewer. The status enum has no
# "submitted" state by design; portal submission lives on a different rail
# (FetchTask + the portal adapters) and is human-gated permanently.
# ---------------------------------------------------------------------------


class SubmissionPackage(Base):
    """A bundle of question drafts for one tender. Created on the first
    POST to the draft endpoint and reused by later questions on the same
    tender."""

    __tablename__ = "submission_packages"
    __table_args__ = (
        Index("ix_submission_packages_tender_id", "tender_id"),
        Index("ix_submission_packages_org_id", "org_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tender_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tenders.id", ondelete="CASCADE"),
        nullable=False,
    )
    org_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 'drafting' | 'needs_review'
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="drafting")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    drafts: Mapped[list[SubmissionQuestionDraft]] = relationship(
        back_populates="package",
        cascade="all, delete-orphan",
        order_by="SubmissionQuestionDraft.created_at",
    )


class SubmissionQuestionDraft(Base):
    """One drafted question — the result of running the drafting agent
    against a single template (technical_capability / methodology_delivery /
    social_value / quality_management / risk_contingency in this chunk).

    status:
      'needs_review' — draft passed all blocking validators; human is asked
                       to review and approve.
      'incomplete'   — a blocking validator failed and the agent surfaced
                       it; the draft is stored so the reviewer can see why,
                       but it is NOT signalled as ready.
    There is intentionally NO 'submitted' state on this table — submission
    is a portal-level action behind a Temporal-driven human checkpoint.
    """

    __tablename__ = "submission_question_drafts"
    __table_args__ = (
        Index(
            "ix_submission_question_drafts_package_id", "package_id"
        ),
        Index(
            "ix_submission_question_drafts_tender_id", "tender_id"
        ),
        Index("ix_submission_question_drafts_org_id", "org_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    package_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("submission_packages.id", ondelete="CASCADE"),
        nullable=False,
    )
    tender_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tenders.id", ondelete="CASCADE"),
        nullable=False,
    )
    org_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    template_id: Mapped[str] = mapped_column(String(48), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    # Question reference — label / verbatim text hash. Lets the reviewer
    # see which question this draft answers without storing the verbatim
    # ITT text (copyright).
    question_ref: Mapped[str] = mapped_column(Text, nullable=False)
    structured_content: Mapped[dict | None] = mapped_column(JSONB)
    vault_citations: Mapped[list | None] = mapped_column(JSONB)
    confidence_scores: Mapped[dict | None] = mapped_column(JSONB)
    unfilled_slots: Mapped[list | None] = mapped_column(JSONB)
    validation_report: Mapped[dict | None] = mapped_column(JSONB)
    # 'needs_review' | 'incomplete' — see class docstring.
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="needs_review"
    )
    # Optional model + token usage for cost tracking.
    model: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    package: Mapped[SubmissionPackage] = relationship(back_populates="drafts")


# ---------------------------------------------------------------------------
# Email integration (Phase 6 §5.8): connect an inbox, watch for tender emails,
# file them, draft a reply, notify.
#
# This is the first genuinely MULTI-USER-shaped piece. A connected mailbox
# belongs to ONE Account and its OAuth tokens are isolated to that account;
# one user's email is never visible to another. The OAuth token is a SECRET:
# it is stored encrypted (Fernet, reusing the portal credentials store's key —
# see services/email/token_store.py) in `token_ciphertext` and is NEVER logged.
#
# Read-only OAuth scope by design: the system SUGGESTS a reply but has no send
# capability. Drafts live on MailboxMessage for the user to review and send
# themselves — see CLAUDE.md §7.3 / PROJECT.md §5.8.
# ---------------------------------------------------------------------------


class MailboxAccount(Base):
    """An OAuth-connected inbox belonging to one Account.

    The connect flow is two-step: a `pending` row is created with a random
    `connect_state` when the user starts OAuth, then the provider redirect
    fills `email_address` + `token_ciphertext` and flips status to
    `connected`. Tokens are encrypted at rest and refreshed in place.
    """

    __tablename__ = "mailbox_accounts"
    __table_args__ = (
        # One connection per (account, provider, mailbox). email_address is
        # NULL while pending — Postgres allows multiple NULLs, so concurrent
        # pending connects don't collide.
        UniqueConstraint(
            "account_id",
            "provider",
            "email_address",
            name="uq_mailbox_account_provider_email",
        ),
        Index("ix_mailbox_accounts_account_id", "account_id"),
        Index("ix_mailbox_accounts_connect_state", "connect_state"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 'gmail' | 'outlook' | 'yahoo'
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    # The connected mailbox address, learned from the provider on callback.
    # NULL while the connection is still pending OAuth consent.
    email_address: Mapped[str | None] = mapped_column(String(320))
    # 'pending' | 'connected' | 'disconnected' | 'error'
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )
    # Fernet-encrypted JSON of the OAuth tokens. SECRET — never logged.
    token_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    # Random nonce that ties the provider redirect back to this row.
    connect_state: Mapped[str | None] = mapped_column(String(64))
    # Watermark for the incremental poll: only messages newer than this are
    # listed (minus a small overlap). Advanced after each successful poll.
    last_polled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    account: Mapped[Account] = relationship()
    messages: Mapped[list[MailboxMessage]] = relationship(
        back_populates="mailbox",
        cascade="all, delete-orphan",
    )


class MailboxMessage(Base):
    """A processed inbound email that matched a tender by exact subject ref.

    One row per (mailbox, provider_message_id) — the unique constraint is the
    idempotency guard: re-processing the same message double-files nothing and
    double-notifies no one. We only persist messages we ACTED on (matched a
    tender). Unmatched mail leaves no row by design.

    The suggested reply lives in `draft_reply`. There is no send path: the
    system stores the draft for the user to review and send themselves.
    """

    __tablename__ = "mailbox_messages"
    __table_args__ = (
        UniqueConstraint(
            "mailbox_account_id",
            "provider_message_id",
            name="uq_mailbox_message_provider_id",
        ),
        Index("ix_mailbox_messages_mailbox_account_id", "mailbox_account_id"),
        Index("ix_mailbox_messages_tender_id", "tender_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    mailbox_account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("mailbox_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider_message_id: Mapped[str] = mapped_column(String(256), nullable=False)
    tender_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tenders.id", ondelete="SET NULL"),
    )
    # The exact reference token from the subject that matched the tender.
    matched_ref: Mapped[str | None] = mapped_column(String(256))
    subject: Mapped[str | None] = mapped_column(Text)
    sender: Mapped[str | None] = mapped_column(String(512))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A short stored excerpt of the body (stored, not logged) for the review UI.
    body_excerpt: Mapped[str | None] = mapped_column(Text)
    attachment_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    # Plain-text links found in the body, surfaced to the user but NEVER
    # fetched by the system.
    links: Mapped[list | None] = mapped_column(JSONB)
    draft_reply: Mapped[str | None] = mapped_column(Text)
    # 'drafted' | 'no_reply_needed' | 'skipped' | 'error'
    draft_status: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    mailbox: Mapped[MailboxAccount] = relationship(back_populates="messages")
    tender: Mapped[Tender | None] = relationship()

