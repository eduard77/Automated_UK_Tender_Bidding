"""Per-document-type claims schemas.

Each schema is the **machine-readable summary** of what one document proves.
Stored as JSONB on `vault_document_versions.claims` and queried by the
re-validation engine (`services/vault/matcher.py`).

Design notes
------------

- All claims fields are **Optional** by design. Source documents are messy:
  insurance certs omit territory, ISO certs use creative date formats, case
  studies hide the client behind anonymisation. The extractor returns what
  it can prove; the matcher reasons over what's present.

- Confidence is captured at the **field-list** level, not per-field — each
  model carries `low_confidence_fields: list[str]` listing names the
  extractor wasn't sure about. This keeps the JSONB simple and queryable
  (`claims.cover_amount` is a number, not `{value, confidence}`).

- `notes: list[str]` captures extractor explanations for ambiguity ("issuing
  body printed as logo, transcribed from header line", "expiry date format
  unclear: DD/MM/YYYY vs MM/DD/YYYY", etc.).

- `doc_type` is the discriminator that picks the right model when reading a
  `ClaimsRecord` from the DB.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Enums (frozen vocab; keep in sync with PROJECT.md §5.4)
# ---------------------------------------------------------------------------

InsuranceType = Literal[
    "employers_liability",
    "public_liability",
    "professional_indemnity",
    "product",
    "cyber",
]

PolicyKind = Literal[
    "health_safety",
    "environmental",
    "equality_diversity",
    "modern_slavery",
    "anti_bribery",
    "data_protection",
    "safeguarding",
    "quality",
    "business_continuity",
]

ClientSector = Literal[
    "healthcare",
    "education",
    "local_government",
    "central_government",
    "transport",
    "housing",
    "justice",
    "defence",
    "energy",
    "water",
    "private",
    "third_sector",
    "other",
]

# ---------------------------------------------------------------------------
# Base + per-type models
# ---------------------------------------------------------------------------


class _ClaimsBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    low_confidence_fields: list[str] = Field(
        default_factory=list,
        description="Field names whose extracted value the extractor wasn't confident about.",
    )
    notes: list[str] = Field(
        default_factory=list,
        description="Free-form notes about ambiguity / why a field was omitted.",
    )


class InsuranceCertClaims(_ClaimsBase):
    doc_type: Literal["insurance_certificate"] = "insurance_certificate"

    insurance_type: InsuranceType | None = None
    cover_amount: Decimal | None = None
    currency: str | None = None
    insurer: str | None = None
    insurer_uk_authorised: bool | None = None
    policy_holder: str | None = None
    policy_number: str | None = None
    valid_from: date | None = None
    valid_until: date | None = None
    territory: str | None = None


class IsoCertClaims(_ClaimsBase):
    doc_type: Literal["iso_certificate"] = "iso_certificate"

    standard: str | None = None
    standard_version: str | None = None
    scope: str | None = None
    certifying_body: str | None = None
    certificate_number: str | None = None
    issued_date: date | None = None
    valid_until: date | None = None
    holder: str | None = None


class PolicyClaims(_ClaimsBase):
    doc_type: Literal["policy"] = "policy"

    policy_kind: PolicyKind | None = None
    title: str | None = None
    covers: list[str] = Field(default_factory=list)
    references_standards: list[str] = Field(default_factory=list)
    signed_by_director: bool | None = None
    signatory_name: str | None = None
    signed_date: date | None = None
    review_due: date | None = None


class CaseStudyClaims(_ClaimsBase):
    doc_type: Literal["case_study"] = "case_study"

    client: str | None = None
    client_sector: ClientSector | None = None
    client_anonymised: bool | None = None
    value: Decimal | None = None
    currency: str | None = None
    delivered_from: date | None = None
    delivered_to: date | None = None
    services: list[str] = Field(default_factory=list)
    outcomes: list[str] = Field(default_factory=list)
    team_size: int | None = None
    location: str | None = None
    consent_to_name_client: bool | None = None


class AccountsClaims(_ClaimsBase):
    doc_type: Literal["accounts"] = "accounts"

    fiscal_year_end: date | None = None
    turnover: Decimal | None = None
    currency: str | None = None
    profit_before_tax: Decimal | None = None
    audited: bool | None = None
    auditor: str | None = None


# Discriminated union — use `ClaimsRecord` whenever you want a value that's
# one of the five concrete types, with `doc_type` picking the right one.
ClaimsRecord = Annotated[
    InsuranceCertClaims | IsoCertClaims | PolicyClaims | CaseStudyClaims | AccountsClaims,
    Field(discriminator="doc_type"),
]


DocType = Literal[
    "insurance_certificate",
    "iso_certificate",
    "policy",
    "case_study",
    "accounts",
]


class ClaimsExtractionError(Exception):
    """Raised by extractors when Claude's response can't be parsed into the
    expected schema. The raw model output is attached for debugging."""

    def __init__(self, message: str, raw_output: str | None = None) -> None:
        super().__init__(message)
        self.raw_output = raw_output


__all__ = [
    "AccountsClaims",
    "CaseStudyClaims",
    "ClaimsExtractionError",
    "ClaimsRecord",
    "ClientSector",
    "DocType",
    "InsuranceCertClaims",
    "InsuranceType",
    "IsoCertClaims",
    "PolicyClaims",
    "PolicyKind",
]
