"""Server-side entitlement checks for brief + document access.

The contract: anonymous users and free accounts can see the SEARCH listing
and a HALF-content preview of any brief / document. To see the FULL brief
or download/view full document text, the account needs entitlement for that
specific tender.

ENTITLED for tender T = either:
  1) a `brief_entitlements` row (account_id, T, source in {payg, plan, dev})
     — created when they buy PAYG, when their plan generates a brief, or when
     a dev override is used in non-prod.
  2) an unlimited active plan (`plan_unlimited`) — counts as entitled to ANY
     tender without an explicit row (their per-tender row is still created on
     brief generation so the audit trail stays whole, but viewing doesn't
     require it). Subscriptions expire — current_period_end is the gate.

This module exposes a pure check (`is_entitled`) and a redactor for the
brief_json / document text that the API layer runs before responding. The
redaction is the SOURCE OF TRUTH for what unentitled clients receive — never
trust the frontend to hide locked content.
"""
from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.models import Account, BriefEntitlement

# Named constants — referenced from tests; do NOT inline these magic numbers
# in business logic.
PREVIEW_FRACTION = 0.5  # half the brief, half each document.

# Submission-package fee constants (PART D).
SUBMISSION_FEE_PCT = 0.005  # 0.5% of contract value.
SUBMISSION_FEE_MIN_GBP = 100
SUBMISSION_FEE_MAX_GBP = 300

# Brief keys we expose in the half-preview. Everything else is REDACTED with
# a lock marker — including recommendation, rationale, full key_risks text,
# deadline specifics, full scoring, full mandatory_requirements, etc.
_PREVIEW_BRIEF_KEYS = {"headline", "scope_summary"}

# Plan caps (brief generations per monthly period). 'plan_unlimited' has no
# cap; 'free' / 'payg' don't get to generate via plan at all.
PLAN_MONTHLY_LIMITS = {
    "plan_100": 100,
    "plan_unlimited": None,
}


def is_plan_active(account: Account, *, now: datetime | None = None) -> bool:
    """A subscription plan is active iff the current period hasn't elapsed.
    `payg` and `free` accounts are not 'plan-active' — entitlements drive
    their access."""
    if account.plan not in PLAN_MONTHLY_LIMITS:
        return False
    period_end = account.current_period_end
    if period_end is None:
        return False
    # Some DB backends (SQLite in tests) return naive datetimes; coerce
    # to UTC-aware so the comparison works either way.
    if period_end.tzinfo is None:
        period_end = period_end.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    return period_end > now


def has_entitlement_row(
    db: Session, *, account_id: int, tender_id: int
) -> bool:
    return (
        db.execute(
            select(BriefEntitlement.id).where(
                BriefEntitlement.account_id == account_id,
                BriefEntitlement.tender_id == tender_id,
            )
        ).first()
        is not None
    )


def is_entitled(
    db: Session, *, account: Account | None, tender_id: int
) -> bool:
    """The single check the API uses. Anonymous => False.

    plan_unlimited bypasses the per-row check while the subscription is
    active so we don't have to backfill an entitlement row on every view.
    plan_100 still needs an entitlement row (created on first generation
    against that tender) — that's how the cap meters per-tender.
    """
    if account is None:
        return False
    if account.plan == "plan_unlimited" and is_plan_active(account):
        return True
    return has_entitlement_row(
        db, account_id=account.id, tender_id=tender_id
    )


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def redact_brief_json(brief_json: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the half-preview shape that an unentitled client may see.

    Allow-list rather than deny-list: we keep `headline`, `scope_summary`, and
    aggregate counts. Everything else is removed — recommendation, rationale,
    full key_risks text, deadline specifics, full scoring, full mandatory
    requirements list, raw figures. The response carries `locked: True` plus
    a counts block so the UI can show "3 key risks identified" without
    leaking the risk text.

    NEVER return a key-stripped clone that still carries the original list
    elements — the function returns a fresh dict and only the allow-listed
    pieces are copied.
    """
    if brief_json is None:
        return None

    preview: dict[str, Any] = {"locked": True}
    for key in _PREVIEW_BRIEF_KEYS:
        if key in brief_json and brief_json[key] is not None:
            preview[key] = copy.deepcopy(brief_json[key])

    counts: dict[str, int] = {}
    for key in (
        "key_risks",
        "mandatory_requirements",
        "notable_conditions",
        "missing_or_unclear",
    ):
        value = brief_json.get(key)
        if isinstance(value, list):
            counts[f"{key}_count"] = len(value)
    scoring = brief_json.get("scoring")
    if isinstance(scoring, dict):
        criteria = scoring.get("criteria")
        if isinstance(criteria, list):
            counts["scoring_criteria_count"] = len(criteria)
    if counts:
        preview["counts"] = counts

    preview["unlock"] = {
        "reason": "brief_locked",
        "message": (
            "Full brief is locked. Unlock with a single brief (£10) or a "
            "monthly plan."
        ),
    }
    return preview


def half_preview_text(text: str | None) -> tuple[str, bool]:
    """Return (preview_text, was_truncated) — half the text, with a lock
    marker appended. Anything that's empty or short enough that halving
    leaves nothing useful is returned as the empty preview."""
    if not text:
        return "", False
    cut = int(len(text) * PREVIEW_FRACTION)
    if cut <= 0:
        return "", True
    if cut >= len(text):
        return text, False
    return (
        text[:cut]
        + "\n\n[--- Document locked. Unlock with a single brief (£10) "
        "or a monthly plan to see the full text and download. ---]"
    ), True


# ---------------------------------------------------------------------------
# Submission-package fee
# ---------------------------------------------------------------------------


def submission_package_fee_pence(contract_value: float | int | None) -> int:
    """Compute the upfront submission-package fee in pence.

    Rules (PART D):
    - fee = round(0.5% of contract_value)
    - clamped to [£100, £300]
    - unknown / null / non-positive contract_value => £100 (the floor).
    """
    floor_pence = SUBMISSION_FEE_MIN_GBP * 100
    ceiling_pence = SUBMISSION_FEE_MAX_GBP * 100
    if contract_value is None:
        return floor_pence
    try:
        value = float(contract_value)
    except (TypeError, ValueError):
        return floor_pence
    if value <= 0:
        return floor_pence
    raw_pence = round(value * SUBMISSION_FEE_PCT * 100)
    return max(floor_pence, min(ceiling_pence, raw_pence))


# ---------------------------------------------------------------------------
# Plan metering helpers (used by api/tender_brief generation flow)
# ---------------------------------------------------------------------------


class PlanLimitReached(Exception):  # noqa: N818 — domain-condition name reads more naturally without Error suffix
    """plan_100 has consumed its monthly allowance."""


def grant_entitlement(
    db: Session,
    *,
    account: Account,
    tender_id: int,
    source: str,
) -> BriefEntitlement:
    """Idempotent — if the row already exists, returns it. Used by webhook
    handlers (payg) and the brief-generation flow (plan) and the dev override
    (dev). Does NOT consume the plan allowance — that's a separate call."""
    existing = db.execute(
        select(BriefEntitlement).where(
            BriefEntitlement.account_id == account.id,
            BriefEntitlement.tender_id == tender_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    row = BriefEntitlement(
        account_id=account.id, tender_id=tender_id, source=source
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def consume_plan_generation_if_new(
    db: Session, *, account: Account, tender_id: int
) -> tuple[bool, BriefEntitlement | None]:
    """Generation flow for plan_100 / plan_unlimited.

    Returns (was_new_generation, entitlement_row). Re-generating against an
    already-entitled tender returns (False, existing) and does NOT consume
    any allowance. New tender => create entitlement (source='plan'), bump
    counter (plan_100 only), and return (True, new_row).

    Raises:
        PlanLimitReached if plan_100 has already used 100 generations in
        the current period.
        ValueError if the account isn't on an active brief plan.
    """
    if account.plan not in PLAN_MONTHLY_LIMITS or not is_plan_active(account):
        raise ValueError("account_not_on_active_plan")

    existing = db.execute(
        select(BriefEntitlement).where(
            BriefEntitlement.account_id == account.id,
            BriefEntitlement.tender_id == tender_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return False, existing

    limit = PLAN_MONTHLY_LIMITS[account.plan]
    if limit is not None and account.brief_generations_this_period >= limit:
        raise PlanLimitReached()

    row = BriefEntitlement(
        account_id=account.id, tender_id=tender_id, source="plan"
    )
    db.add(row)
    if limit is not None:
        account.brief_generations_this_period += 1
    db.commit()
    db.refresh(row)
    return True, row


def reset_period_usage(
    account: Account, *, current_period_end: datetime
) -> None:
    """Called from the Stripe webhook when a new period starts."""
    account.brief_generations_this_period = 0
    account.period_anchor = datetime.now(UTC)
    account.current_period_end = current_period_end
