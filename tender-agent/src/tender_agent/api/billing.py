"""Billing API — Stripe Checkout sessions + webhook receiver.

Env-gated: if any required Stripe key is absent, every endpoint here returns
a "payments_not_configured" payload (200) instead of failing. The dashboard
uses that to swap pay buttons for a graceful "Coming soon" state.

The submission-package fee is computed server-side at request time from the
tender's contract_value (see `services.accounts.entitlement
.submission_package_fee_pence`) — never trusted from the client. The Stripe
line item is built dynamically; there is NO pre-made Stripe price for it.
"""
from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from tender_agent.api.deps import require_account
from tender_agent.db import get_db
from tender_agent.models import Account, Tender
from tender_agent.services.accounts.entitlement import (
    submission_package_fee_pence,
)
from tender_agent.services.billing import (
    PaymentsNotConfigured,
    create_payg_brief_session,
    create_plan_session,
    create_submission_package_session,
    handle_webhook_event,
    is_payments_configured,
    verify_webhook_signature,
)
from tender_agent.services.billing.stripe_service import (
    WebhookSignatureError,
    has_submission_package_paid,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])


class CheckoutRequest(BaseModel):
    """Polymorphic checkout body — exactly one of the optional fields drives
    which Stripe session we create."""

    kind: str = Field(
        ..., description="payg_brief | plan_100 | plan_unlimited | submission_package"
    )
    tender_id: int | None = None


class CheckoutResponse(BaseModel):
    status: str  # "ok" | "payments_not_configured"
    url: str | None = None
    session_id: str | None = None
    amount_pence: int | None = None
    message: str | None = None


class SubmissionFeeQuote(BaseModel):
    tender_id: int
    amount_pence: int
    amount_gbp: float
    currency: str = "GBP"
    payments_configured: bool
    already_paid: bool


@router.get(
    "/submission-fee/{tender_id}", response_model=SubmissionFeeQuote
)
def submission_fee_quote(
    tender_id: int,
    account: Account = Depends(require_account),
    db: Session = Depends(get_db),
) -> SubmissionFeeQuote:
    """Returns the upfront fee the user would pay to generate the submission
    package for this tender. Drives the 'Generate package — £NNN' button on
    the tender page; the actual charge happens on /billing/checkout."""
    tender = db.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(status_code=404, detail="tender not found")
    contract_value = (
        float(tender.value_amount) if tender.value_amount is not None else None
    )
    amount_pence = submission_package_fee_pence(contract_value)
    return SubmissionFeeQuote(
        tender_id=tender_id,
        amount_pence=amount_pence,
        amount_gbp=amount_pence / 100,
        currency="GBP",
        payments_configured=is_payments_configured(),
        already_paid=has_submission_package_paid(
            db, account_id=account.id, tender_id=tender_id
        ),
    )


@router.post("/checkout", response_model=CheckoutResponse)
def checkout(
    body: CheckoutRequest,
    account: Account = Depends(require_account),
    db: Session = Depends(get_db),
) -> CheckoutResponse:
    """Create a Stripe Checkout Session and return its URL. Inert when
    payments aren't configured — returns 200 with `payments_not_configured`
    so the dashboard can show "Coming soon" rather than an error."""
    if not is_payments_configured():
        return CheckoutResponse(
            status="payments_not_configured",
            message=(
                "Stripe is not configured on this deployment. Visit the "
                "admin to add STRIPE_SECRET_KEY / STRIPE_PUBLISHABLE_KEY / "
                "STRIPE_WEBHOOK_SECRET."
            ),
        )

    try:
        if body.kind == "payg_brief":
            if body.tender_id is None:
                raise HTTPException(
                    status_code=400, detail="tender_id_required"
                )
            session = create_payg_brief_session(
                db, account=account, tender_id=body.tender_id
            )
            return CheckoutResponse(
                status="ok",
                url=session["url"],
                session_id=session["id"],
            )
        if body.kind in {"plan_100", "plan_unlimited"}:
            session = create_plan_session(
                db, account=account, plan=body.kind
            )
            return CheckoutResponse(
                status="ok",
                url=session["url"],
                session_id=session["id"],
            )
        if body.kind == "submission_package":
            if body.tender_id is None:
                raise HTTPException(
                    status_code=400, detail="tender_id_required"
                )
            tender = db.get(Tender, body.tender_id)
            if tender is None:
                raise HTTPException(status_code=404, detail="tender_not_found")
            session = create_submission_package_session(
                db, account=account, tender=tender
            )
            return CheckoutResponse(
                status="ok",
                url=session["url"],
                session_id=session["id"],
                amount_pence=session["amount_pence"],
            )
    except PaymentsNotConfigured:
        return CheckoutResponse(status="payments_not_configured")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    raise HTTPException(status_code=400, detail="unknown_checkout_kind")


@router.post("/webhook")
async def webhook(request: Request, db: Session = Depends(get_db)) -> Response:
    """Stripe webhook receiver. Signature is ALWAYS verified — there is no
    skip-verification path, even in dev. Tests post a fake event by
    monkeypatching verify_webhook_signature."""
    payload = await request.body()
    signature = request.headers.get("stripe-signature")
    try:
        event = verify_webhook_signature(payload, signature)
    except WebhookSignatureError as exc:
        logger.warning(
            "billing.webhook_rejected", reason="signature_invalid"
        )
        raise HTTPException(
            status_code=400, detail="invalid_signature"
        ) from exc

    outcome = handle_webhook_event(db, event)
    return Response(
        content=f'{{"received": true, "handled": {str(outcome.get("handled", False)).lower()}}}',
        media_type="application/json",
    )


class BillingStatus(BaseModel):
    payments_configured: bool
    publishable_key: str | None
    plan_prices: dict[str, str]


@router.get("/status", response_model=BillingStatus)
def status() -> BillingStatus:
    """Public discovery endpoint. The dashboard hits this on load to decide
    whether to render pay buttons or 'Coming soon'."""
    from tender_agent.config import settings

    return BillingStatus(
        payments_configured=is_payments_configured(),
        # Publishable key is safe to expose — that's what it's for.
        publishable_key=settings.stripe_publishable_key or None,
        plan_prices={
            "payg_brief": settings.stripe_price_brief_payg,
            "plan_100": settings.stripe_price_plan_100,
            "plan_unlimited": settings.stripe_price_plan_unlim,
        },
    )
