"""Server-side gate, redaction, fee calculation, plan metering.

These tests are the SOURCE OF TRUTH for what unentitled clients are allowed
to see. They assert that locked content is PHYSICALLY ABSENT from the
preview, not merely hidden.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from tender_agent.services.accounts import entitlement, passwords
from tests._billing_fixtures import (
    SAMPLE_BRIEF_JSON,
    make_account,
    make_engine_and_session,
    make_tender,
)


@pytest.fixture(autouse=True)
def _fast_bcrypt(monkeypatch) -> None:
    monkeypatch.setattr(passwords, "_ROUNDS", 4)


@pytest.fixture()
def db():
    _, factory = make_engine_and_session()
    s = factory()
    try:
        yield s
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Fee calculation
# ---------------------------------------------------------------------------


def test_fee_at_or_below_min_clamps_to_100() -> None:
    # 0.5% of £15k = £75, but the floor is £100.
    assert entitlement.submission_package_fee_pence(15_000) == 10_000
    assert entitlement.submission_package_fee_pence(1) == 10_000


def test_fee_in_band_uses_half_percent() -> None:
    # 0.5% of £40,000 = £200, sits inside the [£100, £300] band.
    assert entitlement.submission_package_fee_pence(40_000) == 20_000


def test_fee_above_max_clamps_to_300() -> None:
    # 0.5% of £10M = £50k → clamp to £300.
    assert entitlement.submission_package_fee_pence(10_000_000) == 30_000


def test_fee_unknown_value_defaults_to_min() -> None:
    assert entitlement.submission_package_fee_pence(None) == 10_000


def test_fee_zero_or_negative_defaults_to_min() -> None:
    assert entitlement.submission_package_fee_pence(0) == 10_000
    assert entitlement.submission_package_fee_pence(-500_000) == 10_000


def test_named_constants_match_brief() -> None:
    assert entitlement.PREVIEW_FRACTION == 0.5
    assert entitlement.SUBMISSION_FEE_PCT == 0.005
    assert entitlement.SUBMISSION_FEE_MIN_GBP == 100
    assert entitlement.SUBMISSION_FEE_MAX_GBP == 300


# ---------------------------------------------------------------------------
# is_entitled
# ---------------------------------------------------------------------------


def test_anonymous_is_never_entitled(db) -> None:
    tender = make_tender(db)
    assert entitlement.is_entitled(db, account=None, tender_id=tender.id) is False


def test_free_account_with_no_row_is_not_entitled(db) -> None:
    account = make_account(db)
    tender = make_tender(db)
    assert (
        entitlement.is_entitled(db, account=account, tender_id=tender.id)
        is False
    )


def test_payg_account_with_entitlement_row_is_entitled(db) -> None:
    account = make_account(db, plan="payg")
    tender = make_tender(db)
    entitlement.grant_entitlement(
        db, account=account, tender_id=tender.id, source="payg"
    )
    assert entitlement.is_entitled(db, account=account, tender_id=tender.id)


def test_plan_unlimited_active_is_entitled_without_row(db) -> None:
    account = make_account(db, plan="plan_unlimited", plan_active=True)
    tender = make_tender(db)
    # No entitlement row — unlimited bypasses the per-row check.
    assert entitlement.is_entitled(db, account=account, tender_id=tender.id)


def test_plan_unlimited_expired_is_not_entitled(db) -> None:
    account = make_account(db, plan="plan_unlimited", plan_active=False)
    tender = make_tender(db)
    assert (
        entitlement.is_entitled(db, account=account, tender_id=tender.id)
        is False
    )


def test_plan_100_active_without_row_is_not_entitled(db) -> None:
    account = make_account(db, plan="plan_100", plan_active=True)
    tender = make_tender(db)
    # plan_100 requires a per-tender entitlement row even while active.
    assert (
        entitlement.is_entitled(db, account=account, tender_id=tender.id)
        is False
    )


# ---------------------------------------------------------------------------
# Brief redaction
# ---------------------------------------------------------------------------


def test_redact_brief_returns_locked_marker_and_preview_keys() -> None:
    preview = entitlement.redact_brief_json(SAMPLE_BRIEF_JSON)
    assert preview is not None
    assert preview["locked"] is True
    assert preview["headline"] == SAMPLE_BRIEF_JSON["headline"]
    assert preview["scope_summary"] == SAMPLE_BRIEF_JSON["scope_summary"]


def test_redact_brief_strips_recommendation_and_rationale() -> None:
    preview = entitlement.redact_brief_json(SAMPLE_BRIEF_JSON)
    assert preview is not None
    # The locked fields must be physically ABSENT.
    body = json.dumps(preview)
    assert "Strong scope alignment" not in body  # rationale text
    assert "Tight deadline" not in body  # key_risks detail
    assert preview.get("recommendation") is None
    assert preview.get("rationale") is None
    assert preview.get("key_risks") is None
    assert preview.get("deadline") is None
    assert preview.get("scoring") is None
    assert preview.get("mandatory_requirements") is None


def test_redact_brief_counts_block_carries_aggregates() -> None:
    preview = entitlement.redact_brief_json(SAMPLE_BRIEF_JSON)
    assert preview is not None
    counts = preview["counts"]
    assert counts["key_risks_count"] == 3
    assert counts["mandatory_requirements_count"] == 3
    assert counts["scoring_criteria_count"] == 3


def test_redact_brief_none_passthrough() -> None:
    assert entitlement.redact_brief_json(None) is None


# ---------------------------------------------------------------------------
# Document preview
# ---------------------------------------------------------------------------


def test_half_preview_truncates_to_half() -> None:
    full = "A" * 100 + "B" * 100  # 200 chars; second half is all 'B'.
    preview, truncated = entitlement.half_preview_text(full)
    assert truncated is True
    # Second half ('B's) must be physically absent from the preview.
    assert "B" not in preview.split("[")[0]
    assert "Document locked" in preview


def test_half_preview_empty_input() -> None:
    preview, truncated = entitlement.half_preview_text("")
    assert preview == ""
    assert truncated is False
    preview, truncated = entitlement.half_preview_text(None)
    assert preview == ""
    assert truncated is False


# ---------------------------------------------------------------------------
# Plan metering
# ---------------------------------------------------------------------------


def test_plan_100_consumes_on_new_tender(db) -> None:
    account = make_account(db, plan="plan_100", plan_active=True)
    t = make_tender(db)
    was_new, row = entitlement.consume_plan_generation_if_new(
        db, account=account, tender_id=t.id
    )
    assert was_new is True
    assert row.source == "plan"
    assert account.brief_generations_this_period == 1


def test_plan_100_does_not_double_count_re_view(db) -> None:
    account = make_account(db, plan="plan_100", plan_active=True)
    t = make_tender(db)
    entitlement.consume_plan_generation_if_new(
        db, account=account, tender_id=t.id
    )
    was_new, _ = entitlement.consume_plan_generation_if_new(
        db, account=account, tender_id=t.id
    )
    assert was_new is False
    assert account.brief_generations_this_period == 1


def test_plan_100_blocks_at_limit(db) -> None:
    account = make_account(
        db, plan="plan_100", plan_active=True, generations_used=100
    )
    t = make_tender(db)
    with pytest.raises(entitlement.PlanLimitReached):
        entitlement.consume_plan_generation_if_new(
            db, account=account, tender_id=t.id
        )


def test_plan_unlimited_never_blocks_and_does_not_count(db) -> None:
    account = make_account(
        db, plan="plan_unlimited", plan_active=True, generations_used=999
    )
    t = make_tender(db)
    was_new, row = entitlement.consume_plan_generation_if_new(
        db, account=account, tender_id=t.id
    )
    assert was_new is True
    # Unlimited doesn't increment the counter.
    assert account.brief_generations_this_period == 999


def test_consume_rejects_account_not_on_active_plan(db) -> None:
    account = make_account(db, plan="free")
    t = make_tender(db)
    with pytest.raises(ValueError):
        entitlement.consume_plan_generation_if_new(
            db, account=account, tender_id=t.id
        )


def test_payg_grant_idempotent(db) -> None:
    account = make_account(db, plan="payg")
    t = make_tender(db)
    a = entitlement.grant_entitlement(
        db, account=account, tender_id=t.id, source="payg"
    )
    b = entitlement.grant_entitlement(
        db, account=account, tender_id=t.id, source="payg"
    )
    assert a.id == b.id


def test_reset_period_usage_clears_counter() -> None:
    # Pure unit — no DB needed; build a transient Account.
    from tender_agent.models import Account

    a = Account(email="x", password_hash="x", plan="plan_100")
    a.brief_generations_this_period = 50
    new_end = datetime.now(UTC) + timedelta(days=30)
    entitlement.reset_period_usage(a, current_period_end=new_end)
    assert a.brief_generations_this_period == 0
    assert a.current_period_end == new_end
