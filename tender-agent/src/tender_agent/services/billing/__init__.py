"""Stripe (test-mode) integration.

ENV-GATED + INERT WITHOUT KEYS. The api/billing.py endpoints check
`is_payments_configured()` before doing anything that would call Stripe; if
the keys aren't there, they return a clear "payments_not_configured" response
and the rest of the app stays usable. Gates run off plan + entitlement + the
dev override flag.

Pricing model:
* Plans (plan_100 / plan_unlimited) — Checkout in `subscription` mode against
  the pre-created prices in settings.
* PAYG single brief — Checkout in `payment` mode against the pre-created
  one-off price.
* Submission package — Checkout in `payment` mode with a DYNAMIC line item
  (`price_data` built on the fly) for the computed amount in pence. There is
  no pre-made Stripe price for it; the amount is server-computed at request
  time from the tender's contract_value via
  `services.accounts.entitlement.submission_package_fee_pence`.
"""
from tender_agent.services.billing.stripe_service import (  # noqa: F401
    PaymentsNotConfigured,
    create_payg_brief_session,
    create_plan_session,
    create_submission_package_session,
    handle_webhook_event,
    is_payments_configured,
    verify_webhook_signature,
)
