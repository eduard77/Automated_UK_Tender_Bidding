"""Red-warning detection from brief fields + vault.

The four spec red-warnings:
    mandatory_requirement_unmet
    cannot_deliver_to_standard
    likely_unprofitable
    tight_deadline

EVERY warning here is derived from data that's already in the brief or in
the vault — we never re-call the LLM and we never invent a finding. Where
the data is genuinely unknown the engine emits a `please_confirm` entry in
`missing_info` instead of fabricating a warning.

Tight-deadline thresholds are configurable via a single named constant
(`TIGHT_DEADLINE_WORKING_DAYS`) so the dashboard can override without code
changes if needed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from tender_agent.models import Tender, TenderBrief
from tender_agent.services.go_no_go.reconciliation import (
    ReconciliationResult,
)

TIGHT_DEADLINE_WORKING_DAYS = 5


@dataclass
class RedWarning:
    warning: str        # spec id, e.g. "tight_deadline"
    detail: str         # 1-sentence reason the dashboard can show
    # When the warning was derived from a specific requirement, name it so
    # the self-certification "qualifies" question can prefill against it.
    requirement: str | None = None
    # Vault row(s) that grounded the warning, when any.
    source_document: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "warning": self.warning,
            "detail": self.detail,
        }
        if self.requirement is not None:
            out["requirement"] = self.requirement
        if self.source_document is not None:
            out["source_document"] = self.source_document
        return out


@dataclass
class MissingInfo:
    criterion: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"criterion": self.criterion, "reason": self.reason}


@dataclass
class WarningsResult:
    red_warnings: list[RedWarning] = field(default_factory=list)
    missing_info: list[MissingInfo] = field(default_factory=list)
    working_days_remaining: int | None = None


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------


def detect_red_warnings(
    *,
    tender: Tender,
    brief: TenderBrief,
    reconciliation: ReconciliationResult,
    today: date | None = None,
) -> WarningsResult:
    """Produce the red_warnings + missing_info pair the engine outputs.
    Pass an already-computed reconciliation so the "mandatory unmet" case
    can re-use its findings without re-reading the vault."""
    today = today or date.today()
    result = WarningsResult()

    _check_mandatory_requirements(brief, reconciliation, result)
    _check_tight_deadline(tender, brief, today, result)
    _check_likely_unprofitable(brief, result)
    _check_cannot_deliver_to_standard(brief, result)

    return result


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


def _check_mandatory_requirements(
    brief: TenderBrief,
    recon: ReconciliationResult,
    result: WarningsResult,
) -> None:
    """A `shortfall` or `contradiction` row in reconciliation means we have
    PROOF the requirement isn't met — that's a red warning. An `expiry` row
    is also a red warning because the evidenced cert lapses before contract
    end. `please_confirm` rows do NOT escalate — they go to missing_info."""
    for w in recon.warnings:
        if w.kind in {"shortfall", "contradiction"} or w.kind == "expiry":
            result.red_warnings.append(
                RedWarning(
                    warning="mandatory_requirement_unmet",
                    detail=w.detail,
                    requirement=w.requirement,
                    source_document=w.source_document,
                )
            )

    for prompt in recon.please_confirm:
        result.missing_info.append(
            MissingInfo(
                criterion="mandatory_requirement_evidence",
                reason=f"{prompt.requirement}: {prompt.detail}",
            )
        )


def _check_tight_deadline(
    tender: Tender,
    brief: TenderBrief,
    today: date,
    result: WarningsResult,
) -> None:
    deadline = _resolve_deadline(tender, brief)
    if deadline is None:
        result.missing_info.append(
            MissingInfo(
                criterion="submission_deadline",
                reason="No submission deadline found on the tender record or in the brief.",
            )
        )
        return
    if deadline < today:
        # Already past — still flag, the client may be reviewing a closed
        # opportunity for awareness.
        result.red_warnings.append(
            RedWarning(
                warning="tight_deadline",
                detail=(
                    f"Submission deadline {deadline.isoformat()} has already "
                    "passed."
                ),
            )
        )
        result.working_days_remaining = 0
        return
    days = _working_days_between(today, deadline)
    result.working_days_remaining = days
    if days <= TIGHT_DEADLINE_WORKING_DAYS:
        result.red_warnings.append(
            RedWarning(
                warning="tight_deadline",
                detail=(
                    f"Only {days} working day{'s' if days != 1 else ''} "
                    f"until deadline ({deadline.isoformat()})."
                ),
            )
        )


def _check_likely_unprofitable(
    brief: TenderBrief, result: WarningsResult
) -> None:
    """We don't have a `BidPricingHistory` table yet. Per the spec, with no
    comparable pricing history we flag profit as unmodelled and reflect that
    in confidence — we do NOT fabricate an unprofitable warning. So this
    check looks for an EXPLICIT signal in the brief's key_risks before
    raising the red warning."""
    body = brief.brief_json or {}
    risks = body.get("key_risks") or []
    for risk in risks:
        title = (risk.get("title") or "").lower() if isinstance(risk, dict) else ""
        detail = (risk.get("detail") or "").lower() if isinstance(risk, dict) else ""
        blob = f"{title} {detail}"
        if (
            "unprofitable" in blob
            or "margin" in blob
            or "loss-making" in blob
            or "below cost" in blob
            or "price floor" in blob
        ):
            result.red_warnings.append(
                RedWarning(
                    warning="likely_unprofitable",
                    detail=risk.get("detail") or risk.get("title") or "",
                )
            )
            return
    # No pricing history table exists; per the spec, mark unmodelled.
    result.missing_info.append(
        MissingInfo(
            criterion="profit_modelling",
            reason=(
                "No comparable pricing history available. Profitability is "
                "unmodelled — please review the contract value vs your cost "
                "base before bidding."
            ),
        )
    )


def _check_cannot_deliver_to_standard(
    brief: TenderBrief, result: WarningsResult
) -> None:
    body = brief.brief_json or {}
    risks = body.get("key_risks") or []
    for risk in risks:
        title = (risk.get("title") or "").lower() if isinstance(risk, dict) else ""
        detail = (risk.get("detail") or "").lower() if isinstance(risk, dict) else ""
        blob = f"{title} {detail}"
        if any(
            phrase in blob
            for phrase in (
                "cannot deliver",
                "can't deliver",
                "capacity gap",
                "no capability",
                "lacks capability",
                "lack the capability",
                "insufficient capacity",
                "outside our capability",
                "outside capability",
            )
        ):
            result.red_warnings.append(
                RedWarning(
                    warning="cannot_deliver_to_standard",
                    detail=risk.get("detail") or risk.get("title") or "",
                )
            )
            return
    # No explicit signal — leave it to the client (please_confirm via
    # self-certification "has_documents" question). Not added to missing_info
    # explicitly to avoid pestering on every tender.


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def _resolve_deadline(tender: Tender, brief: TenderBrief) -> date | None:
    if tender.deadline_at is not None:
        return tender.deadline_at.date()
    body = brief.brief_json or {}
    deadline_block = body.get("deadline")
    if isinstance(deadline_block, dict):
        raw = deadline_block.get("by")
        if isinstance(raw, str) and raw.strip():
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
            except ValueError:
                return None
    return None


def _working_days_between(start: date, end: date) -> int:
    """Inclusive end, exclusive start. Saturday=5, Sunday=6 excluded. Does
    NOT account for UK bank holidays — that's a known approximation; the
    five-working-day threshold is a heuristic anyway."""
    if end <= start:
        return 0
    days = 0
    cursor = start + timedelta(days=1)
    while cursor <= end:
        if cursor.weekday() < 5:
            days += 1
        cursor = cursor + timedelta(days=1)
    return days
