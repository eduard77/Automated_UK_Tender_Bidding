"""Email API: provider status, the connect flow, OAuth callback, and per-account
scoping. Offline: in-memory SQLite, settings monkeypatched, provider faked.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tender_agent.api import email as email_api
from tender_agent.config import settings
from tender_agent.db import get_db
from tender_agent.main import app
from tender_agent.models import Account, MailboxAccount
from tender_agent.services.accounts import passwords
from tests._auth_helpers import authenticate_unlimited
from tests._billing_fixtures import make_engine_and_session
from tests._email_fixtures import FakeProvider, use_fake_store


@pytest.fixture(autouse=True)
def _fast_bcrypt(monkeypatch) -> None:
    monkeypatch.setattr(passwords, "_ROUNDS", 4)


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


def _configure_gmail(monkeypatch) -> None:
    monkeypatch.setattr(settings, "gmail_client_id", "cid")
    monkeypatch.setattr(settings, "gmail_client_secret", "sec")
    monkeypatch.setattr(
        settings, "email_oauth_redirect_uri", "https://api.test/email/oauth/callback"
    )


def test_list_providers(client) -> None:
    r = client.get("/email/providers")
    assert r.status_code == 200
    by_name = {p["provider"]: p for p in r.json()}
    assert set(by_name) == {"gmail", "outlook", "yahoo"}
    assert by_name["yahoo"]["implemented"] is False
    assert by_name["gmail"]["implemented"] is True


def test_connect_requires_auth(client) -> None:
    assert client.post("/email/connect/gmail").status_code == 401


def test_connect_unconfigured_returns_clear_message(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "gmail_client_id", "")
    authenticate_unlimited(client)
    r = client.post("/email/connect/gmail")
    assert r.status_code == 503
    assert "not configured yet" in r.json()["detail"]


def test_connect_configured_returns_auth_url_and_pending_row(
    client, db, monkeypatch
) -> None:
    _configure_gmail(monkeypatch)
    authenticate_unlimited(client)
    r = client.post("/email/connect/gmail")
    assert r.status_code == 200
    assert "accounts.google.com" in r.json()["authorization_url"]
    pending = db.execute(
        select(MailboxAccount).where(MailboxAccount.status == "pending")
    ).scalars().all()
    assert len(pending) == 1
    assert pending[0].provider == "gmail"


def test_unknown_provider_404(client, monkeypatch) -> None:
    authenticate_unlimited(client)
    assert client.post("/email/connect/nope").status_code == 404


def test_oauth_callback_connects_mailbox(
    client, db, monkeypatch, tmp_path
) -> None:
    _configure_gmail(monkeypatch)
    use_fake_store(monkeypatch, tmp_path)
    # Fake the provider so no network is touched on exchange/get_address.
    monkeypatch.setattr(
        email_api, "build_provider", lambda name, **kw: FakeProvider()
    )
    authenticate_unlimited(client)
    client.post("/email/connect/gmail")
    state = db.execute(
        select(MailboxAccount).where(MailboxAccount.status == "pending")
    ).scalar_one().connect_state

    r = client.get(
        "/email/oauth/callback",
        params={"state": state, "code": "auth-code"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "connected=gmail" in r.headers["location"]

    db.expire_all()
    mb = db.execute(
        select(MailboxAccount).where(MailboxAccount.status == "connected")
    ).scalar_one()
    assert mb.email_address == "me@example.com"
    assert mb.token_ciphertext is not None
    assert mb.connect_state is None


def test_oauth_callback_rejects_unknown_state(client, monkeypatch) -> None:
    authenticate_unlimited(client)
    r = client.get(
        "/email/oauth/callback",
        params={"state": "bogus", "code": "x"},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_connections_are_scoped_per_account(client, db, monkeypatch) -> None:
    authenticate_unlimited(client)
    me = db.execute(select(Account)).scalars().one()  # the only signed-up acct
    other = Account(email="other@example.com", password_hash="x", plan="free")
    db.add(other)
    db.commit()
    db.refresh(other)

    # A connected mailbox for ME and one for the OTHER account.
    db.add(
        MailboxAccount(
            account_id=me.id,
            provider="gmail",
            email_address="me@gmail.com",
            status="connected",
            token_ciphertext=b"x",
        )
    )
    db.add(
        MailboxAccount(
            account_id=other.id,
            provider="outlook",
            email_address="other@outlook.com",
            status="connected",
            token_ciphertext=b"y",
        )
    )
    db.commit()

    r = client.get("/email/connections")
    assert r.status_code == 200
    emails = {c["email_address"] for c in r.json()}
    assert emails == {"me@gmail.com"}  # never the other account's inbox


def test_cannot_touch_another_accounts_mailbox(client, db, monkeypatch) -> None:
    authenticate_unlimited(client)
    other = Account(email="other2@example.com", password_hash="x", plan="free")
    db.add(other)
    db.commit()
    db.refresh(other)
    foreign = MailboxAccount(
        account_id=other.id,
        provider="gmail",
        email_address="other@gmail.com",
        status="connected",
        token_ciphertext=b"z",
    )
    db.add(foreign)
    db.commit()
    db.refresh(foreign)

    # 404 (not 403) so we never confirm the foreign mailbox exists.
    assert client.delete(f"/email/connections/{foreign.id}").status_code == 404
    assert (
        client.get(f"/email/connections/{foreign.id}/messages").status_code
        == 404
    )


def test_disconnect_clears_token(client, db, monkeypatch) -> None:
    authenticate_unlimited(client)
    me = db.execute(select(Account)).scalars().one()
    mb = MailboxAccount(
        account_id=me.id,
        provider="gmail",
        email_address="me@gmail.com",
        status="connected",
        token_ciphertext=b"secret",
    )
    db.add(mb)
    db.commit()
    db.refresh(mb)

    assert client.delete(f"/email/connections/{mb.id}").status_code == 204
    db.expire_all()
    refreshed = db.get(MailboxAccount, mb.id)
    assert refreshed.status == "disconnected"
    assert refreshed.token_ciphertext is None
