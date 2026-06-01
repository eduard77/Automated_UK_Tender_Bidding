"""Shared fixtures for the chunk-6 (accounts/billing/gating) tests.

We use an in-memory SQLite engine so the suite runs without Postgres. The
ORM is mostly Postgres-flavoured (JSONB, ARRAY) — SQLite copes via the type
adaptation SQLAlchemy provides; rows that exercise those columns just get
stored as JSON / native lists. None of the gating logic touches Postgres
features.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from tender_agent.models import (
    Account,
    Base,
    Source,
    Tender,
    TenderBrief,
    TenderDocumentFile,
)
from tender_agent.services.accounts.auth import hash_password


# SQLite compat shims for the Postgres-flavoured types used by the
# tenders table. We only need them at DDL time; the rows we create in
# tests never read the ARRAY/Vector columns back.
@compiles(ARRAY, "sqlite")
def _array_sqlite(_element, _compiler, **_kw):
    return "TEXT"


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(_element, _compiler, **_kw):
    return "TEXT"


# SQLite only auto-increments INTEGER primary keys, not BIGINT — map BigInteger
# to INTEGER for the test schema so primary keys are populated automatically.
@compiles(BigInteger, "sqlite")
def _bigint_sqlite(_element, _compiler, **_kw):
    return "INTEGER"


try:
    from pgvector.sqlalchemy import Vector as _Vector

    @compiles(_Vector, "sqlite")
    def _vector_sqlite(_element, _compiler, **_kw):
        return "BLOB"

except Exception:  # pragma: no cover — pgvector optional in tests
    pass


def make_engine_and_session():
    """Build a fresh in-memory SQLite engine with our schema. StaticPool keeps
    the same connection for the whole test so the in-memory DB persists
    across sessions."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, future=True
    )
    return engine, factory


def make_account(
    db: Session,
    *,
    email: str = "user@example.com",
    plan: str = "free",
    plan_active: bool = False,
    generations_used: int = 0,
    stripe_customer_id: str | None = None,
) -> Account:
    account = Account(
        email=email,
        password_hash=hash_password("Password123"),
        plan=plan,
        stripe_customer_id=stripe_customer_id,
        brief_generations_this_period=generations_used,
    )
    if plan_active:
        account.current_period_end = datetime.now(UTC) + timedelta(days=20)
        account.period_anchor = datetime.now(UTC)
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def make_tender(
    db: Session,
    *,
    source_code: str = "FTS",
    source_ref: str = "test-tender-1",
    title: str = "Example tender",
    value_amount: float | None = 200_000.0,
) -> Tender:
    src = (
        db.query(Source)
        .filter(Source.code == source_code)
        .one_or_none()
    )
    if src is None:
        src = Source(
            code=source_code,
            name=f"{source_code} source",
            base_url="https://example.test",
        )
        db.add(src)
        db.commit()
    tender = Tender(
        source_code=source_code,
        source_ref=source_ref,
        title=title,
        value_amount=value_amount,
    )
    db.add(tender)
    db.commit()
    db.refresh(tender)
    return tender


SAMPLE_BRIEF_JSON = {
    "recommendation": "bid",
    "confidence": "high",
    "headline": "Bid: clean cyber-security retrofit",
    "rationale": "Strong scope alignment; deliverable in our team's wheelhouse.",
    "key_risks": [
        {"title": "Tight deadline", "detail": "Just 14 days to respond."},
        {"title": "Mandatory ISO 27001", "detail": "We hold it — but include cert."},
        {"title": "30-day payment terms", "detail": "Cash-flow constraint."},
    ],
    "deadline": {"by": "2026-08-01T12:00:00Z", "notes": "Strict cutoff."},
    "contract_value": {"amount": 200000, "currency": "GBP"},
    "mandatory_requirements": [
        "ISO 27001",
        "Cyber Essentials Plus",
        "3 years of audited accounts",
    ],
    "scoring": {
        "summary": "60% quality / 40% price",
        "criteria": [
            {"label": "Technical", "weight": 40},
            {"label": "Approach", "weight": 20},
            {"label": "Price", "weight": 40},
        ],
    },
    "scope_summary": (
        "Replace the legacy SOC tooling, retain SIEM, provide 24/7 cover."
    ),
    "notable_conditions": ["No subcontracting without consent."],
    "missing_or_unclear": ["Estimated number of endpoints not stated."],
}


def make_brief(
    db: Session,
    *,
    tender_id: int,
    brief_json: dict | None = None,
) -> TenderBrief:
    payload = brief_json if brief_json is not None else SAMPLE_BRIEF_JSON
    brief = TenderBrief(
        tender_id=tender_id,
        status="complete",
        recommendation=payload.get("recommendation"),
        confidence=payload.get("confidence"),
        headline=payload.get("headline"),
        brief_json=payload,
        generated_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(brief)
    db.commit()
    db.refresh(brief)
    return brief


def make_document(
    db: Session,
    *,
    tender_id: int,
    text: str = "A" * 200,
    sha256: str = "sha-1",
    title: str = "ITT.pdf",
) -> TenderDocumentFile:
    doc = TenderDocumentFile(
        tender_id=tender_id,
        url="https://example.test/itt.pdf",
        title=title,
        format="pdf",
        bytes=len(text),
        sha256=sha256,
        text_extracted=text,
        download_status="ok",
        storage_key=None,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc
