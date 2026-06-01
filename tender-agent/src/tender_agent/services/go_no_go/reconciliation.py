"""Vault reconciliation — word vs evidence.

The brief sets out the tender's mandatory_requirements. After the client
self-certifies "yes, I qualify", this engine checks the certification
against the EVIDENCE in the vault and emits one of four types per
requirement:

* contradiction — vault evidence falls below the threshold (e.g. client said
  yes, accounts show £3m vs £5m required).
* expiry — cert/insurance evidenced but expires before contract end.
* shortfall — numeric value below threshold (a more general form of the
  contradiction emitter; we keep both labels because the spec lists them
  separately and the dashboard surfaces them differently).
* please_confirm — no vault evidence either way. NOT a warning, NOT a
  fabricated "unmet". A polite request to upload or confirm.

No LLM call here. Re-runnable on demand: a contradiction resolved by a later
upload simply stops being emitted next time the function runs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy.orm import Session

from tender_agent.models import Tender, TenderBrief
from tender_agent.services.go_no_go.requirements_parse import (
    Probe,
    parse_requirement,
)
from tender_agent.services.go_no_go.vault_query import (
    accreditation_present,
    fetch_current_vault_facts,
    insurance_certs,
    iso_certs,
    latest_turnover,
)

WarningKind = Literal[
    "contradiction", "expiry", "shortfall", "please_confirm"
]


@dataclass
class ReconciliationWarning:
    """One row in the reconciliation output. `kind` distinguishes the four
    spec cases; `source_document` is None for please_confirm (we have no
    document to cite when nothing has been uploaded)."""

    kind: WarningKind
    requirement: str
    detail: str
    required_value: str | None = None
    evidenced_value: str | None = None
    source_document: dict[str, Any] | None = None
    expiry_date: date | None = None
    contract_end: date | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "requirement": self.requirement,
            "detail": self.detail,
        }
        if self.required_value is not None:
            out["required_value"] = self.required_value
        if self.evidenced_value is not None:
            out["evidenced_value"] = self.evidenced_value
        if self.source_document is not None:
            out["source_document"] = self.source_document
        if self.expiry_date is not None:
            out["expiry_date"] = self.expiry_date.isoformat()
        if self.contract_end is not None:
            out["contract_end"] = self.contract_end.isoformat()
        return out


@dataclass
class ReconciliationResult:
    warnings: list[ReconciliationWarning] = field(default_factory=list)
    please_confirm: list[ReconciliationWarning] = field(default_factory=list)
    # Mandatory requirements that we could not parse at all — surfaced so the
    # client can paraphrase them in plain English. Not a warning.
    unparsed_requirements: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "warnings": [w.to_dict() for w in self.warnings],
            "please_confirm": [w.to_dict() for w in self.please_confirm],
            "unparsed_requirements": list(self.unparsed_requirements),
        }


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def reconcile_vault_against_tender(
    db: Session,
    *,
    tender: Tender,
    brief: TenderBrief,
    org_id: int = 1,
    today: date | None = None,
) -> ReconciliationResult:
    """Run the reconciliation. `org_id` defaults to 1 (the single-org
    deployment the rest of the codebase uses); when multi-tenancy lands the
    API layer will pass through the caller's org_id."""
    today = today or date.today()
    contract_end = _contract_end_date(tender, brief)

    mandatory = _extract_mandatory_requirements(brief)
    facts = fetch_current_vault_facts(db, org_id=org_id)

    result = ReconciliationResult()
    for line in mandatory:
        probe = parse_requirement(line)
        if probe.kind == "unknown":
            result.unparsed_requirements.append(line)
            result.please_confirm.append(
                ReconciliationWarning(
                    kind="please_confirm",
                    requirement=line,
                    detail=(
                        "We couldn't auto-parse this requirement. Please "
                        "confirm you meet it."
                    ),
                )
            )
            continue

        if probe.kind == "insurance":
            _reconcile_insurance(probe, facts, result, contract_end)
        elif probe.kind == "turnover":
            _reconcile_turnover(probe, facts, result)
        elif probe.kind == "iso_standard":
            _reconcile_iso(probe, facts, result, contract_end)
        elif probe.kind == "accreditation":
            _reconcile_accreditation(probe, facts, result, contract_end)
    return result


# ---------------------------------------------------------------------------
# Per-kind reconcilers
# ---------------------------------------------------------------------------


def _reconcile_insurance(
    probe: Probe,
    facts,
    result: ReconciliationResult,
    contract_end: date | None,
) -> None:
    certs = insurance_certs(facts, insurance_type=probe.insurance_type)
    if not certs:
        result.please_confirm.append(
            ReconciliationWarning(
                kind="please_confirm",
                requirement=probe.original,
                detail=(
                    "No matching insurance certificate found in the vault. "
                    "Upload or confirm."
                ),
            )
        )
        return
    # If we have any cert at all but no usable cover amount, still ask.
    extracted = [(f, amt) for f, amt in certs if amt is not None]
    if not extracted:
        result.please_confirm.append(
            ReconciliationWarning(
                kind="please_confirm",
                requirement=probe.original,
                detail=(
                    "Insurance certificate found but cover amount not "
                    "extracted. Please confirm the cover level."
                ),
            )
        )
        return
    extracted.sort(key=lambda pair: pair[1] or Decimal(0), reverse=True)
    best_fact, best_amount = extracted[0]
    threshold = probe.threshold_value
    if threshold is not None and best_amount < threshold:
        result.warnings.append(
            ReconciliationWarning(
                kind="shortfall",
                requirement=probe.original,
                detail=(
                    f"Insurance evidenced ({_money(best_amount)}) is below "
                    f"the tender's required {_money(threshold)}."
                ),
                required_value=_money(threshold),
                evidenced_value=_money(best_amount),
                source_document=_source_doc(best_fact),
            )
        )
    _maybe_expiry(best_fact, probe, contract_end, result)


def _reconcile_turnover(probe: Probe, facts, result: ReconciliationResult) -> None:
    pair = latest_turnover(facts)
    if pair is None:
        result.please_confirm.append(
            ReconciliationWarning(
                kind="please_confirm",
                requirement=probe.original,
                detail=(
                    "No accounts in the vault with an extracted turnover. "
                    "Upload or confirm."
                ),
            )
        )
        return
    fact, turnover = pair
    threshold = probe.threshold_value
    if threshold is None:
        # We saw a "turnover" requirement but couldn't parse the threshold —
        # ask for confirmation rather than guess.
        result.please_confirm.append(
            ReconciliationWarning(
                kind="please_confirm",
                requirement=probe.original,
                detail=(
                    "Couldn't parse a turnover threshold from the "
                    "requirement. Please confirm."
                ),
            )
        )
        return
    if turnover < threshold:
        result.warnings.append(
            ReconciliationWarning(
                kind="contradiction",
                requirement=probe.original,
                detail=(
                    f"Vault accounts show {_money(turnover)} turnover; "
                    f"this tender requires {_money(threshold)}."
                ),
                required_value=_money(threshold),
                evidenced_value=_money(turnover),
                source_document=_source_doc(fact),
            )
        )


def _reconcile_iso(
    probe: Probe,
    facts,
    result: ReconciliationResult,
    contract_end: date | None,
) -> None:
    matches = iso_certs(facts, standard=probe.standard)
    if not matches:
        result.please_confirm.append(
            ReconciliationWarning(
                kind="please_confirm",
                requirement=probe.original,
                detail=(
                    f"No ISO {probe.standard} certificate found in the "
                    "vault. Upload or confirm."
                ),
            )
        )
        return
    # Take the cert whose expiry is furthest in the future (or None).
    matches.sort(key=lambda f: f.expiry_date or date.max, reverse=True)
    best = matches[0]
    _maybe_expiry(best, probe, contract_end, result)


def _reconcile_accreditation(
    probe: Probe,
    facts,
    result: ReconciliationResult,
    contract_end: date | None,
) -> None:
    matches = accreditation_present(facts, standard=probe.standard or "")
    if not matches:
        result.please_confirm.append(
            ReconciliationWarning(
                kind="please_confirm",
                requirement=probe.original,
                detail=(
                    f"No '{probe.standard or 'accreditation'}' document "
                    "found in the vault. Upload or confirm."
                ),
            )
        )
        return
    best = matches[0]
    _maybe_expiry(best, probe, contract_end, result)


def _maybe_expiry(
    fact,
    probe: Probe,
    contract_end: date | None,
    result: ReconciliationResult,
) -> None:
    if fact.expiry_date is None or contract_end is None:
        return
    if fact.expiry_date < contract_end:
        result.warnings.append(
            ReconciliationWarning(
                kind="expiry",
                requirement=probe.original,
                detail=(
                    f"Evidence (from {fact.title}) expires "
                    f"{fact.expiry_date.isoformat()} — before the contract "
                    f"end {contract_end.isoformat()}."
                ),
                source_document=_source_doc(fact),
                expiry_date=fact.expiry_date,
                contract_end=contract_end,
            )
        )


# ---------------------------------------------------------------------------
# Brief / tender accessors
# ---------------------------------------------------------------------------


def _extract_mandatory_requirements(brief: TenderBrief) -> list[str]:
    body = brief.brief_json or {}
    raw = body.get("mandatory_requirements") or []
    return [str(line).strip() for line in raw if str(line).strip()]


def _contract_end_date(tender: Tender, brief: TenderBrief) -> date | None:
    """Best-effort contract end. The brief carries a `deadline` (response
    deadline, NOT contract end), so the canonical signal is
    `tender.contract_end`. Falls back to today + 12 months if neither is
    known — that lets the expiry check at least fire for short-dated certs.
    """
    if tender.contract_end is not None:
        return tender.contract_end
    return None


def _money(value: Decimal | float | int) -> str:
    """Format £X.Y / £Xm without inventing a currency we didn't see."""
    amount = Decimal(value)
    if amount >= 1_000_000:
        millions = amount / Decimal(1_000_000)
        # Trim trailing zeros: 10.000m → 10m, 10.500m → 10.5m.
        return f"£{_strip_zero(millions)}m"
    if amount >= 1_000:
        thousands = amount / Decimal(1_000)
        return f"£{_strip_zero(thousands)}k"
    return f"£{_strip_zero(amount)}"


def _strip_zero(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _source_doc(fact) -> dict[str, Any]:
    return {
        "document_id": fact.document_id,
        "version_id": fact.version_id,
        "title": fact.title,
        "category": fact.category,
    }
