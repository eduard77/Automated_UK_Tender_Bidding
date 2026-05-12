"""ORM models. Canonical schema for tenders normalised across sources."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

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
    buyer_country: Mapped[str | None] = mapped_column(String(8))
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
