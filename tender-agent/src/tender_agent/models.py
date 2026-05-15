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
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY
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
    """Browser Web Push subscription. Anonymous by endpoint — no user accounts yet.

    A subscription with `filter_profile_id IS NULL` receives every new match,
    regardless of which filter triggered it. A subscription bound to a specific
    profile receives only matches against that profile.
    """

    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
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

