"""Stripe service (test mode) — checkout-session creators + webhook dispatch.

The Stripe SDK is replaced with a fake module so the test suite makes ZERO
network calls. Webhook signature verification is also short-circuited by
swapping `verify_webhook_signature` — we still call it in
test_webhook_rejects_unsigned to prove unsigned events are 400'd.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tender_agent.config import settings
from tender_agent.models import (
    BriefEntitlement,
    SubmissionPackagePayment,
)
from tender_agent.services.accounts import passwords
from tender_agent.services.billing import stripe_service
from tests._billing_fixtures import (
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


@pytest.fixture()
def stripe_configured(monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_dummy")
    monkeypatch.setattr(settings, "stripe_publishable_key", "pk_test_dummy")
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_dummy")
    monkeypatch.setattr(
        settings, "stripe_price_brief_payg", "price_payg_test"
    )
    monkeypatch.setattr(settings, "stripe_price_plan_100", "price_p100_test")
    monkeypatch.setattr(
        settings, "stripe_price_plan_unlim", "price_punl_test"
    )


class _FakeCustomer:
    def __init__(self) -> None:
        self.created: list[dict] = []

    def create(self, **kwargs):
        self.created.append(kwargs)
        return {"id": f"cus_test_{len(self.created)}"}


class _FakeCheckoutSession:
    def __init__(self) -> None:
        self.created: list[dict] = []

    def create(self, **kwargs):
        self.created.append(kwargs)
        return {
            "id": f"cs_test_{len(self.created)}",
            "url": "https://checkout.stripe.com/c/pay/test",
        }


class _FakeWebhook:
    def __init__(self) -> None:
        self.verify_calls: list[tuple] = []
        self.accept: bool = True

    def construct_event(self, payload, signature, secret):
        self.verify_calls.append((payload, signature, secret))
        if not self.accept:
            raise ValueError("bad signature")
        # echo back a stub event
        return {"type": "checkout.session.completed", "data": {"object": {}}}


@pytest.fixture()
def fake_stripe(monkeypatch):
    fake = SimpleNamespace(
        Customer=_FakeCustomer(),
        checkout=SimpleNamespace(Session=_FakeCheckoutSession()),
        Webhook=_FakeWebhook(),
        api_key=None,
    )
    monkeypatch.setattr(stripe_service, "_stripe_client", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# Configuration gate
# ---------------------------------------------------------------------------


def test_is_payments_configured_false_without_keys(monkeypatch) -> None:
    monkeypatch.setattr(settings, "stripe_secret_key", "")
    monkeypatch.setattr(settings, "stripe_publishable_key", "")
    monkeypatch.setattr(settings, "stripe_webhook_secret", "")
    assert stripe_service.is_payments_configured() is False


def test_is_payments_configured_true_with_keys(stripe_configured) -> None:
    assert stripe_service.is_payments_configured() is True


def test_create_payg_raises_when_not_configured(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "stripe_secret_key", "")
    monkeypatch.setattr(settings, "stripe_publishable_key", "")
    monkeypatch.setattr(settings, "stripe_webhook_secret", "")
    account = make_account(db)
    with pytest.raises(stripe_service.PaymentsNotConfigured):
        stripe_service.create_payg_brief_session(
            db, account=account, tender_id=1
        )


# ---------------------------------------------------------------------------
# Checkout sessions
# ---------------------------------------------------------------------------


def test_create_payg_session_uses_payg_price_and_metadata(
    db, stripe_configured, fake_stripe
) -> None:
    account = make_account(db)
    tender = make_tender(db, value_amount=50_000)
    result = stripe_service.create_payg_brief_session(
        db, account=account, tender_id=tender.id
    )
    call = fake_stripe.checkout.Session.created[-1]
    assert call["mode"] == "payment"
    assert call["line_items"][0]["price"] == "price_payg_test"
    assert call["metadata"]["kind"] == "payg_brief"
    assert call["metadata"]["tender_id"] == str(tender.id)
    assert call["metadata"]["account_id"] == str(account.id)
    assert result["url"].startswith("https://checkout.stripe.com/")
    assert account.stripe_customer_id is not None


def test_create_plan_session_subscription_mode(
    db, stripe_configured, fake_stripe
) -> None:
    account = make_account(db)
    stripe_service.create_plan_session(
        db, account=account, plan="plan_100"
    )
    call = fake_stripe.checkout.Session.created[-1]
    assert call["mode"] == "subscription"
    assert call["line_items"][0]["price"] == "price_p100_test"
    assert call["metadata"]["kind"] == "plan_100"


def test_create_plan_session_rejects_unknown_plan(
    db, stripe_configured, fake_stripe
) -> None:
    account = make_account(db)
    with pytest.raises(ValueError):
        stripe_service.create_plan_session(
            db, account=account, plan="not_a_plan"
        )


def test_submission_package_uses_dynamic_price_data(
    db, stripe_configured, fake_stripe
) -> None:
    account = make_account(db)
    # £40,000 → 0.5% = £200 → amount_pence 20_000
    tender = make_tender(db, value_amount=40_000)
    result = stripe_service.create_submission_package_session(
        db, account=account, tender=tender
    )
    call = fake_stripe.checkout.Session.created[-1]
    item = call["line_items"][0]
    # No pre-made Stripe price — price_data is the source of truth.
    assert "price" not in item
    assert item["price_data"]["unit_amount"] == 20_000
    assert item["price_data"]["currency"] == "gbp"
    assert call["metadata"]["kind"] == "submission_package"
    assert result["amount_pence"] == 20_000
    # SubmissionPackagePayment row was created pending.
    payment = (
        db.query(SubmissionPackagePayment)
        .filter_by(account_id=account.id, tender_id=tender.id)
        .one()
    )
    assert payment.status == "pending"
    assert payment.amount_pence == 20_000


def test_submission_package_clamps_high_value(
    db, stripe_configured, fake_stripe
) -> None:
    account = make_account(db)
    tender = make_tender(db, value_amount=10_000_000)
    result = stripe_service.create_submission_package_session(
        db, account=account, tender=tender
    )
    # 0.5% of £10M → £50k, clamped to £300 (30000p).
    assert result["amount_pence"] == 30_000


def test_submission_package_unknown_value_uses_floor(
    db, stripe_configured, fake_stripe
) -> None:
    account = make_account(db)
    tender = make_tender(db, value_amount=None)
    result = stripe_service.create_submission_package_session(
        db, account=account, tender=tender
    )
    assert result["amount_pence"] == 10_000  # £100 floor


# ---------------------------------------------------------------------------
# Webhook dispatch
# ---------------------------------------------------------------------------


def test_webhook_unsigned_raises(monkeypatch, stripe_configured) -> None:
    with pytest.raises(stripe_service.WebhookSignatureError):
        stripe_service.verify_webhook_signature(b"{}", None)


def test_webhook_payg_brief_grants_entitlement(db) -> None:
    account = make_account(db)
    tender = make_tender(db)
    event = {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "metadata": {
                    "kind": "payg_brief",
                    "account_id": str(account.id),
                    "tender_id": str(tender.id),
                },
            }
        },
    }
    outcome = stripe_service.handle_webhook_event(db, event)
    assert outcome["handled"] is True
    rows = db.query(BriefEntitlement).all()
    assert len(rows) == 1
    assert rows[0].source == "payg"
    assert rows[0].account_id == account.id
    assert rows[0].tender_id == tender.id


def test_webhook_plan_activates_subscription_and_resets_usage(db) -> None:
    account = make_account(
        db, plan="free", generations_used=42
    )
    event = {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "metadata": {
                    "kind": "plan_100",
                    "account_id": str(account.id),
                },
                "customer": "cus_x1",
                "subscription_period_end": int(
                    (datetime.now(UTC) + timedelta(days=30)).timestamp()
                ),
            }
        },
    }
    stripe_service.handle_webhook_event(db, event)
    db.refresh(account)
    assert account.plan == "plan_100"
    assert account.brief_generations_this_period == 0
    assert account.current_period_end is not None
    assert account.stripe_customer_id == "cus_x1"


def test_webhook_submission_package_flips_to_paid(db) -> None:
    account = make_account(db)
    tender = make_tender(db, value_amount=200_000)
    payment = SubmissionPackagePayment(
        account_id=account.id,
        tender_id=tender.id,
        amount_pence=30_000,
        currency="GBP",
        status="pending",
        stripe_checkout_session_id="cs_test_99",
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)

    event = {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "metadata": {
                    "kind": "submission_package",
                    "account_id": str(account.id),
                    "tender_id": str(tender.id),
                    "submission_payment_id": str(payment.id),
                },
                "payment_intent": "pi_test_99",
            }
        },
    }
    stripe_service.handle_webhook_event(db, event)
    db.refresh(payment)
    assert payment.status == "paid"
    assert payment.paid_at is not None
    assert payment.stripe_payment_intent_id == "pi_test_99"


def test_webhook_subscription_deleted_drops_plan(db) -> None:
    account = make_account(
        db, plan="plan_100", plan_active=True, stripe_customer_id="cus_drop"
    )
    event = {
        "type": "customer.subscription.deleted",
        "data": {"object": {"customer": "cus_drop"}},
    }
    stripe_service.handle_webhook_event(db, event)
    db.refresh(account)
    assert account.plan == "free"
    assert account.current_period_end is None


def test_webhook_unknown_event_is_acked_unhandled(db) -> None:
    outcome = stripe_service.handle_webhook_event(
        db, {"type": "invoice.created", "data": {"object": {}}}
    )
    assert outcome["handled"] is False
