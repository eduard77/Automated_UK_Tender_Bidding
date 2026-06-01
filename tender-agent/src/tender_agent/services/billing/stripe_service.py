"""Stripe (test mode) — Checkout Sessions + webhook handling.

EVERY function in this module is safe to import even when Stripe isn't
configured; the gate is `is_payments_configured()`. Endpoints check it before
doing anything Stripe-side and return a clean "payments_not_configured" state
otherwise. No keys are ever logged.

Webhook signatures are ALWAYS verified — there is no "skip verification" path,
and an unsigned/wrong-signature event is rejected with a 400.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.config import settings
from tender_agent.models import (
    Account,
    BriefEntitlement,
    SubmissionPackagePayment,
    Tender,
)
from tender_agent.services.accounts.entitlement import (
    grant_entitlement,
    reset_period_usage,
    submission_package_fee_pence,
)

logger = structlog.get_logger(__name__)


class PaymentsNotConfigured(Exception):  # noqa: N818 — domain-condition name, not an error suffix style
    """Raised when an endpoint tried to create a Stripe session but the keys
    are missing. The API layer maps to a 200 with `payments_not_configured`
    so the dashboard can show 'Coming soon' gracefully."""


class WebhookSignatureError(Exception):
    """Raised when the signature header is missing/invalid. The API maps to
    400; we never process unsigned webhooks."""


def is_payments_configured() -> bool:
    """All three Stripe secrets must be present. Price IDs ship with sane
    test-mode defaults so we don't check them — Stripe will surface a clear
    error if they're wrong."""
    return bool(
        settings.stripe_secret_key
        and settings.stripe_publishable_key
        and settings.stripe_webhook_secret
    )


def _stripe_client():
    """Lazy import — keeps the test suite import-clean when stripe isn't
    installed and lets us swap in a fake in tests via monkeypatching."""
    import stripe  # local import on purpose

    stripe.api_key = settings.stripe_secret_key
    return stripe


def _success_url() -> str:
    return settings.stripe_success_url or (
        settings.dashboard_base_url.rstrip("/") + "/billing/success"
    )


def _cancel_url() -> str:
    return settings.stripe_cancel_url or (
        settings.dashboard_base_url.rstrip("/") + "/billing/cancel"
    )


def _ensure_customer(db: Session, account: Account) -> str:
    """Create the Stripe customer once and cache the id on the account row.
    The customer's email is set so the dashboard knows who paid; we never
    push extra PII."""
    if account.stripe_customer_id:
        return account.stripe_customer_id
    stripe = _stripe_client()
    customer = stripe.Customer.create(email=account.email)
    account.stripe_customer_id = customer["id"]
    db.commit()
    return account.stripe_customer_id


# ---------------------------------------------------------------------------
# Checkout session creators
# ---------------------------------------------------------------------------


def create_payg_brief_session(
    db: Session, *, account: Account, tender_id: int
) -> dict[str, Any]:
    """One-off £10 PAYG brief unlock. Stripe mode: payment.

    Metadata carries `kind=payg_brief` + `account_id` + `tender_id` so the
    webhook can find the right (account, tender) pair when the session
    completes.
    """
    if not is_payments_configured():
        raise PaymentsNotConfigured()
    stripe = _stripe_client()
    customer_id = _ensure_customer(db, account)
    session = stripe.checkout.Session.create(
        mode="payment",
        customer=customer_id,
        line_items=[
            {"price": settings.stripe_price_brief_payg, "quantity": 1}
        ],
        success_url=_success_url(),
        cancel_url=_cancel_url(),
        metadata={
            "kind": "payg_brief",
            "account_id": str(account.id),
            "tender_id": str(tender_id),
        },
    )
    return {"id": session["id"], "url": session["url"]}


def create_plan_session(
    db: Session, *, account: Account, plan: str
) -> dict[str, Any]:
    """plan_100 (£100/mo) or plan_unlimited (£250/mo). Mode: subscription."""
    if not is_payments_configured():
        raise PaymentsNotConfigured()
    if plan == "plan_100":
        price = settings.stripe_price_plan_100
    elif plan == "plan_unlimited":
        price = settings.stripe_price_plan_unlim
    else:
        raise ValueError(f"unknown plan: {plan!r}")

    stripe = _stripe_client()
    customer_id = _ensure_customer(db, account)
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": price, "quantity": 1}],
        success_url=_success_url(),
        cancel_url=_cancel_url(),
        metadata={
            "kind": plan,
            "account_id": str(account.id),
        },
    )
    return {"id": session["id"], "url": session["url"]}


def create_submission_package_session(
    db: Session, *, account: Account, tender: Tender
) -> dict[str, Any]:
    """Submission-package upfront fee. DYNAMIC line item via price_data —
    there is NO pre-made Stripe price for this product because the amount
    depends on the tender's contract_value.

    Persists a SubmissionPackagePayment(status='pending') so the webhook can
    flip it to 'paid' and unlock generation. The amount is server-computed,
    never trusted from the client.
    """
    if not is_payments_configured():
        raise PaymentsNotConfigured()

    contract_value = (
        float(tender.value_amount) if tender.value_amount is not None else None
    )
    amount_pence = submission_package_fee_pence(contract_value)

    payment = SubmissionPackagePayment(
        account_id=account.id,
        tender_id=tender.id,
        amount_pence=amount_pence,
        currency="GBP",
        status="pending",
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)

    stripe = _stripe_client()
    customer_id = _ensure_customer(db, account)
    session = stripe.checkout.Session.create(
        mode="payment",
        customer=customer_id,
        line_items=[
            {
                "quantity": 1,
                "price_data": {
                    "currency": "gbp",
                    "unit_amount": amount_pence,
                    "product_data": {
                        "name": "Submission package",
                        "description": (
                            f"Bid submission package for tender #{tender.id}"
                        ),
                    },
                },
            }
        ],
        success_url=_success_url(),
        cancel_url=_cancel_url(),
        metadata={
            "kind": "submission_package",
            "account_id": str(account.id),
            "tender_id": str(tender.id),
            "submission_payment_id": str(payment.id),
        },
    )
    payment.stripe_checkout_session_id = session["id"]
    db.commit()
    return {
        "id": session["id"],
        "url": session["url"],
        "amount_pence": amount_pence,
    }


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


def verify_webhook_signature(
    payload: bytes, signature_header: str | None
) -> dict[str, Any]:
    """Verify the Stripe-Signature header against the raw body. Raises
    WebhookSignatureError on any failure — the API maps that to 400 and we
    NEVER process an unsigned/wrong-signature event. The webhook secret is
    read from settings; it's required even in test mode."""
    if not signature_header:
        raise WebhookSignatureError("missing_signature")
    if not settings.stripe_webhook_secret:
        raise WebhookSignatureError("webhook_secret_not_configured")
    stripe = _stripe_client()
    try:
        return stripe.Webhook.construct_event(
            payload, signature_header, settings.stripe_webhook_secret
        )
    except Exception as exc:  # noqa: BLE001 — stripe raises various types
        raise WebhookSignatureError(str(exc)) from exc


def handle_webhook_event(db: Session, event: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a verified webhook to the per-event handler. Returns a small
    dict the API can echo back ({'handled': True, 'kind': ...}). Anything we
    don't care about returns {'handled': False, ...} and is acked with 200."""
    event_type = event.get("type")
    if event_type == "checkout.session.completed":
        return _on_checkout_session_completed(db, event["data"]["object"])
    if event_type in {
        "customer.subscription.updated",
        "customer.subscription.deleted",
    }:
        return _on_subscription_change(db, event["data"]["object"], event_type)
    return {"handled": False, "type": event_type}


def _on_checkout_session_completed(
    db: Session, session: dict[str, Any]
) -> dict[str, Any]:
    metadata = session.get("metadata") or {}
    kind = metadata.get("kind")
    account_id_raw = metadata.get("account_id")
    if not account_id_raw:
        return {"handled": False, "reason": "no_account_id"}
    account = db.get(Account, int(account_id_raw))
    if account is None:
        return {"handled": False, "reason": "account_not_found"}

    if kind == "payg_brief":
        tender_id = int(metadata["tender_id"])
        grant_entitlement(
            db, account=account, tender_id=tender_id, source="payg"
        )
        # Bump plan to 'payg' if they're still 'free' — it just tracks that
        # they've ever paid for a one-off. Doesn't affect access (which keys
        # off entitlement rows for non-subscription accounts).
        if account.plan == "free":
            account.plan = "payg"
            db.commit()
        logger.info(
            "billing.payg_brief_unlocked",
            account_id=account.id,
            tender_id=tender_id,
        )
        return {"handled": True, "kind": "payg_brief"}

    if kind in {"plan_100", "plan_unlimited"}:
        # The subscription.updated event will arrive separately with the
        # canonical period_end — but reset usage now so the user can
        # generate immediately even if the second event is delayed.
        period_end_raw = session.get("subscription_period_end")
        if period_end_raw:
            period_end = datetime.fromtimestamp(int(period_end_raw), tz=UTC)
        else:
            period_end = datetime.now(UTC).replace(microsecond=0)
        account.plan = kind
        reset_period_usage(account, current_period_end=period_end)
        # Also remember the subscription's stripe customer id if missing.
        customer_id = session.get("customer")
        if customer_id and not account.stripe_customer_id:
            account.stripe_customer_id = customer_id
        db.commit()
        logger.info(
            "billing.plan_activated", account_id=account.id, plan=kind
        )
        return {"handled": True, "kind": kind}

    if kind == "submission_package":
        submission_payment_id = int(metadata["submission_payment_id"])
        payment = db.get(SubmissionPackagePayment, submission_payment_id)
        if payment is None:
            return {"handled": False, "reason": "submission_payment_missing"}
        payment.status = "paid"
        payment.paid_at = datetime.now(UTC)
        payment.stripe_payment_intent_id = session.get("payment_intent")
        db.commit()
        logger.info(
            "billing.submission_package_paid",
            account_id=account.id,
            tender_id=payment.tender_id,
        )
        return {"handled": True, "kind": "submission_package"}

    return {"handled": False, "kind": kind}


def _on_subscription_change(
    db: Session, subscription: dict[str, Any], event_type: str
) -> dict[str, Any]:
    customer_id = subscription.get("customer")
    if not customer_id:
        return {"handled": False, "reason": "no_customer"}
    account = db.execute(
        select(Account).where(Account.stripe_customer_id == customer_id)
    ).scalar_one_or_none()
    if account is None:
        return {"handled": False, "reason": "account_not_found"}

    if event_type == "customer.subscription.deleted":
        account.plan = "free"
        account.current_period_end = None
        db.commit()
        logger.info("billing.subscription_deleted", account_id=account.id)
        return {"handled": True, "kind": "subscription_deleted"}

    # updated — keep the period end fresh; flip plan if Stripe price changed.
    items = (subscription.get("items") or {}).get("data") or []
    plan_from_price: str | None = None
    for item in items:
        price_id = (item.get("price") or {}).get("id")
        if price_id == settings.stripe_price_plan_100:
            plan_from_price = "plan_100"
        elif price_id == settings.stripe_price_plan_unlim:
            plan_from_price = "plan_unlimited"
    period_end_raw = subscription.get("current_period_end")
    period_end = (
        datetime.fromtimestamp(int(period_end_raw), tz=UTC)
        if period_end_raw
        else None
    )
    if plan_from_price:
        account.plan = plan_from_price
    if period_end is not None:
        if (
            account.current_period_end is None
            or period_end > account.current_period_end
        ):
            # New period — reset the counter.
            reset_period_usage(account, current_period_end=period_end)
        else:
            account.current_period_end = period_end
    if subscription.get("status") == "canceled":
        account.plan = "free"
        account.current_period_end = None
    db.commit()
    logger.info(
        "billing.subscription_updated",
        account_id=account.id,
        plan=account.plan,
    )
    return {"handled": True, "kind": "subscription_updated"}


# ---------------------------------------------------------------------------
# Helpers exposed for the dashboard / tests
# ---------------------------------------------------------------------------


def has_submission_package_paid(
    db: Session, *, account_id: int, tender_id: int
) -> bool:
    """True iff there's a paid (not cancelled/pending) submission-package
    payment for this (account, tender)."""
    return (
        db.execute(
            select(SubmissionPackagePayment.id).where(
                SubmissionPackagePayment.account_id == account_id,
                SubmissionPackagePayment.tender_id == tender_id,
                SubmissionPackagePayment.status == "paid",
            )
        ).first()
        is not None
    )


def latest_entitlement(
    db: Session, *, account_id: int, tender_id: int
) -> BriefEntitlement | None:
    return db.execute(
        select(BriefEntitlement).where(
            BriefEntitlement.account_id == account_id,
            BriefEntitlement.tender_id == tender_id,
        )
    ).scalar_one_or_none()
