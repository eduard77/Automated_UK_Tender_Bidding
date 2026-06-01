"""End-to-end API tests for the brief + document gates and the billing
endpoints. Uses an in-memory SQLite DB; the Stripe SDK is replaced with a
fake so the suite makes zero network calls.

Tests assert that locked content is PHYSICALLY ABSENT from the response body
for unentitled callers — see test_brief_redacts_recommendation_for_anonymous.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tender_agent.api import tender_brief as tb_mod
from tender_agent.config import settings
from tender_agent.db import get_db
from tender_agent.main import app
from tender_agent.services.accounts import passwords
from tender_agent.services.billing import stripe_service
from tests._billing_fixtures import (
    make_account,
    make_brief,
    make_document,
    make_engine_and_session,
    make_tender,
)


@pytest.fixture(autouse=True)
def _fast_bcrypt(monkeypatch) -> None:
    monkeypatch.setattr(passwords, "_ROUNDS", 4)


@pytest.fixture(autouse=True)
def _no_real_brief_generation(monkeypatch) -> None:
    async def _noop(brief_id: int, tender_id: int) -> None:
        return None

    monkeypatch.setattr(tb_mod, "schedule_brief", _noop)


@pytest.fixture()
def db_factory():
    _, factory = make_engine_and_session()
    return factory


@pytest.fixture()
def client(db_factory):
    def _override():
        s = db_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def db(db_factory):
    s = db_factory()
    try:
        yield s
    finally:
        s.close()


def _signup_and_login(client: TestClient, email: str, password: str) -> dict:
    r = client.post(
        "/accounts/signup", json={"email": email, "password": password}
    )
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Anonymous + free preview path
# ---------------------------------------------------------------------------


def test_anonymous_get_brief_returns_redacted_preview(client, db) -> None:
    tender = make_tender(db)
    make_brief(db, tender_id=tender.id)
    r = client.get(f"/tenders/{tender.id}/brief")
    assert r.status_code == 200
    body = r.json()
    # Locked fields are physically absent in the JSON.
    assert body["recommendation"] is None
    assert body["brief_json"]["locked"] is True
    raw = json.dumps(body)
    # Rationale and risk-detail strings must not appear anywhere.
    assert "Strong scope alignment" not in raw
    assert "Tight deadline" not in raw
    # Allow-listed keys still present.
    assert body["brief_json"]["scope_summary"].startswith(
        "Replace the legacy SOC"
    )
    assert body["brief_json"]["counts"]["key_risks_count"] == 3


def test_anonymous_document_content_is_half(client, db) -> None:
    tender = make_tender(db)
    # First half is all 'A's, second half is all 'B's. We assert no 'B' makes
    # it back to the unentitled client — that's the literal "second half is
    # not on the wire" guarantee.
    full = "A" * 500 + "B" * 500
    doc = make_document(db, tender_id=tender.id, text=full)
    r = client.get(f"/tenders/{tender.id}/documents/{doc.id}/content")
    assert r.status_code == 200
    body = r.json()
    assert body["locked"] is True
    # Second half ('B's) must be physically absent from the response.
    assert "B" not in body["text"]
    assert "Document locked" in body["text"]
    assert body["preview_chars"] < body["char_count"]


def test_anonymous_download_returns_402(client, db, tmp_path) -> None:
    tender = make_tender(db)
    doc = make_document(db, tender_id=tender.id)
    # storage_key needs to be set for the download endpoint to even get to
    # the gate's downstream checks, but the gate runs FIRST so we don't need
    # a real file on disk.
    r = client.get(f"/tenders/{tender.id}/documents/{doc.id}/file")
    assert r.status_code == 402


# ---------------------------------------------------------------------------
# Entitled paths
# ---------------------------------------------------------------------------


def test_payg_entitled_account_sees_full_brief(client, db) -> None:
    tender = make_tender(db)
    make_brief(db, tender_id=tender.id)
    me = _signup_and_login(client, "payg@example.com", "Password123")
    # Use dev override to grant the entitlement (Stripe-free testing).
    r = client.post(
        "/accounts/dev/override",
        json={"account_id": me["id"], "grant_tender_id": tender.id},
    )
    assert r.status_code == 200
    r = client.get(f"/tenders/{tender.id}/brief")
    assert r.status_code == 200
    body = r.json()
    # Recommendation + rationale come back UN-redacted.
    assert body["recommendation"] == "bid"
    assert "Strong scope alignment" in json.dumps(body)
    assert body["brief_json"]["rationale"].startswith("Strong scope alignment")
    assert body["brief_json"].get("locked") is None


def test_plan_unlimited_account_sees_full_brief_without_row(client, db) -> None:
    tender = make_tender(db)
    make_brief(db, tender_id=tender.id)
    me = _signup_and_login(client, "unlim@example.com", "Password123")
    client.post(
        "/accounts/dev/override",
        json={"account_id": me["id"], "plan": "plan_unlimited"},
    )
    r = client.get(f"/tenders/{tender.id}/brief")
    body = r.json()
    assert body["recommendation"] == "bid"
    assert body["brief_json"].get("locked") is None


def test_plan_100_account_locked_without_entitlement(client, db) -> None:
    tender = make_tender(db)
    make_brief(db, tender_id=tender.id)
    me = _signup_and_login(client, "p100@example.com", "Password123")
    client.post(
        "/accounts/dev/override",
        json={"account_id": me["id"], "plan": "plan_100"},
    )
    # No entitlement row yet → preview path.
    r = client.get(f"/tenders/{tender.id}/brief")
    assert r.json()["brief_json"]["locked"] is True


# ---------------------------------------------------------------------------
# Generation flow
# ---------------------------------------------------------------------------


def test_generate_brief_requires_auth(client, db) -> None:
    tender = make_tender(db)
    r = client.post(f"/tenders/{tender.id}/generate-brief")
    assert r.status_code == 401


def test_generate_brief_402_for_free_account(client, db) -> None:
    tender = make_tender(db)
    _signup_and_login(client, "free@example.com", "Password123")
    r = client.post(f"/tenders/{tender.id}/generate-brief")
    assert r.status_code == 402


def test_generate_brief_consumes_plan_100_on_new_tender(client, db) -> None:
    tender = make_tender(db)
    me = _signup_and_login(client, "meter@example.com", "Password123")
    client.post(
        "/accounts/dev/override",
        json={"account_id": me["id"], "plan": "plan_100"},
    )
    r = client.post(f"/tenders/{tender.id}/generate-brief")
    assert r.status_code == 202
    me_after = client.get("/me").json()
    assert me_after["plan_used"] == 1


def test_generate_brief_does_not_double_count_re_generation(client, db) -> None:
    tender = make_tender(db)
    me = _signup_and_login(client, "rep@example.com", "Password123")
    client.post(
        "/accounts/dev/override",
        json={"account_id": me["id"], "plan": "plan_100"},
    )
    client.post(f"/tenders/{tender.id}/generate-brief")
    client.post(f"/tenders/{tender.id}/generate-brief")
    me_after = client.get("/me").json()
    assert me_after["plan_used"] == 1


def test_generate_brief_blocks_plan_100_at_limit(client, db) -> None:
    tender = make_tender(db)
    me = _signup_and_login(client, "cap@example.com", "Password123")
    client.post(
        "/accounts/dev/override",
        json={"account_id": me["id"], "plan": "plan_100"},
    )
    # Crank counter to 100 via direct DB write — easier than 100 API calls.
    from tender_agent.models import Account

    account = db.query(Account).filter_by(id=me["id"]).one()
    account.brief_generations_this_period = 100
    db.commit()
    r = client.post(f"/tenders/{tender.id}/generate-brief")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "monthly_limit_reached"


# ---------------------------------------------------------------------------
# /me
# ---------------------------------------------------------------------------


def test_me_anonymous_returns_null(client) -> None:
    r = client.get("/me")
    assert r.status_code == 200
    assert r.json() is None


def test_me_authenticated_returns_plan_and_usage(client, db) -> None:
    _signup_and_login(client, "me@example.com", "Password123")
    r = client.get("/me")
    body = r.json()
    assert body["email"] == "me@example.com"
    assert body["plan"] == "free"
    assert body["plan_used"] == 0
    assert body["plan_limit"] is None
    assert body["has_unlimited"] is False


# ---------------------------------------------------------------------------
# Free alerts
# ---------------------------------------------------------------------------


def test_free_alerts_signup_creates_free_account(client) -> None:
    r = client.post(
        "/accounts/free-alerts",
        json={"email": "alert@example.com", "password": "Password123"},
    )
    body = r.json()
    assert r.status_code == 200
    assert body["plan"] == "free"
    assert "Free alerts enabled" in body["message"]
    # Cookie is set so the user is logged in.
    me = client.get("/me").json()
    assert me["email"] == "alert@example.com"


# ---------------------------------------------------------------------------
# Billing endpoints
# ---------------------------------------------------------------------------


def test_billing_status_reports_not_configured_without_keys(
    client, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "stripe_secret_key", "")
    monkeypatch.setattr(settings, "stripe_publishable_key", "")
    monkeypatch.setattr(settings, "stripe_webhook_secret", "")
    r = client.get("/billing/status")
    body = r.json()
    assert body["payments_configured"] is False
    assert body["publishable_key"] is None


def test_billing_checkout_inert_without_keys(client, db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "stripe_secret_key", "")
    monkeypatch.setattr(settings, "stripe_publishable_key", "")
    monkeypatch.setattr(settings, "stripe_webhook_secret", "")
    tender = make_tender(db)
    _signup_and_login(client, "inert@example.com", "Password123")
    r = client.post(
        "/billing/checkout",
        json={"kind": "payg_brief", "tender_id": tender.id},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "payments_not_configured"


def _configure_stripe(monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_dummy")
    monkeypatch.setattr(settings, "stripe_publishable_key", "pk_test_dummy")
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_dummy")


class _FakeCustomer:
    def create(self, **kwargs):
        return {"id": "cus_api_test"}


class _FakeSession:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "id": "cs_api_test",
            "url": "https://checkout.stripe.com/c/api/test",
        }


@pytest.fixture()
def fake_stripe_api(monkeypatch):
    fake = SimpleNamespace(
        Customer=_FakeCustomer(),
        checkout=SimpleNamespace(Session=_FakeSession()),
        Webhook=SimpleNamespace(
            construct_event=lambda payload, sig, secret: json.loads(payload)
        ),
        api_key=None,
    )
    monkeypatch.setattr(stripe_service, "_stripe_client", lambda: fake)
    return fake


def test_billing_checkout_submission_uses_dynamic_amount(
    client, db, monkeypatch, fake_stripe_api
) -> None:
    _configure_stripe(monkeypatch)
    tender = make_tender(db, value_amount=40_000)
    _signup_and_login(client, "ck@example.com", "Password123")
    r = client.post(
        "/billing/checkout",
        json={"kind": "submission_package", "tender_id": tender.id},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    # 0.5% of 40k = £200 = 20000p.
    assert body["amount_pence"] == 20_000
    # Verify the dynamic line item arrived at Stripe.
    last = fake_stripe_api.checkout.Session.calls[-1]
    item = last["line_items"][0]
    assert "price" not in item
    assert item["price_data"]["unit_amount"] == 20_000


def test_billing_webhook_unsigned_rejected(client, monkeypatch) -> None:
    _configure_stripe(monkeypatch)
    r = client.post(
        "/billing/webhook",
        content=b'{"type":"checkout.session.completed"}',
    )
    assert r.status_code == 400


def test_billing_webhook_signed_event_processes(
    client, db, monkeypatch, fake_stripe_api
) -> None:
    _configure_stripe(monkeypatch)
    account = make_account(db, email="hook@example.com")
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
    r = client.post(
        "/billing/webhook",
        content=json.dumps(event).encode(),
        headers={"stripe-signature": "t=1,v1=fake_but_ack"},
    )
    assert r.status_code == 200
    assert r.json()["handled"] is True
    # Entitlement landed in the DB — the brief endpoint would now serve the
    # full row to this account (verified by other tests using the dev path).
    from tender_agent.models import BriefEntitlement

    rows = db.query(BriefEntitlement).filter_by(account_id=account.id).all()
    assert len(rows) == 1


def test_submission_fee_quote_returns_clamp(client, db) -> None:
    tender = make_tender(db, value_amount=10_000_000)
    _signup_and_login(client, "quote@example.com", "Password123")
    r = client.get(f"/billing/submission-fee/{tender.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["amount_pence"] == 30_000  # £300 ceiling
    assert body["amount_gbp"] == 300.0
    assert body["currency"] == "GBP"


# ---------------------------------------------------------------------------
# Dev override safety
# ---------------------------------------------------------------------------


def test_dev_override_refused_in_production(client, db, monkeypatch) -> None:
    # Sign up in dev mode first so the auth cookie is usable (the cookie is
    # marked Secure when env=production, which TestClient over http won't
    # replay). Then flip to production and confirm the override is refused.
    me = _signup_and_login(client, "prod@example.com", "Password123")
    monkeypatch.setattr(settings, "tender_agent_env", "production")
    r = client.post(
        "/accounts/dev/override",
        json={"account_id": me["id"], "plan": "plan_unlimited"},
    )
    assert r.status_code == 403


def test_dev_override_grants_entitlement_in_dev(client, db) -> None:
    tender = make_tender(db)
    me = _signup_and_login(client, "dev@example.com", "Password123")
    r = client.post(
        "/accounts/dev/override",
        json={"account_id": me["id"], "grant_tender_id": tender.id},
    )
    assert r.status_code == 200
    # That tender now serves the full brief to this user.
    make_brief(db, tender_id=tender.id)
    body = client.get(f"/tenders/{tender.id}/brief").json()
    assert body["recommendation"] == "bid"
