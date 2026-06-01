"""Account auth — signup, login, sessions."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tender_agent.services.accounts import auth, passwords
from tests._billing_fixtures import make_engine_and_session


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


def test_signup_creates_free_account(db) -> None:
    account = auth.signup(db, email="A@example.com", password="Password123")
    # Email is lowercased on storage so re-signup isn't an oracle.
    assert account.email == "a@example.com"
    assert account.plan == "free"
    assert account.password_hash.startswith("$2b$")
    # plaintext never persisted.
    assert "Password123" not in account.password_hash


def test_signup_rejects_short_password(db) -> None:
    with pytest.raises(auth.AuthError):
        auth.signup(db, email="a@example.com", password="short")


def test_signup_rejects_duplicate_email(db) -> None:
    auth.signup(db, email="dupe@example.com", password="Password123")
    with pytest.raises(auth.AuthError):
        auth.signup(db, email="DUPE@example.com", password="OtherPass1")


def test_login_round_trip(db) -> None:
    auth.signup(db, email="login@example.com", password="Password123")
    account = auth.login(db, email="LOGIN@example.com", password="Password123")
    assert account.email == "login@example.com"


def test_login_rejects_bad_password(db) -> None:
    auth.signup(db, email="bad@example.com", password="Password123")
    with pytest.raises(auth.AuthError):
        auth.login(db, email="bad@example.com", password="wrong-password")


def test_login_rejects_unknown_email(db) -> None:
    with pytest.raises(auth.AuthError):
        auth.login(db, email="nobody@example.com", password="Password123")


def test_session_round_trip_and_revocation(db) -> None:
    account = auth.signup(db, email="ses@example.com", password="Password123")
    sess = auth.issue_session(db, account)
    assert auth.resolve_session(db, sess.token).id == account.id
    auth.revoke_session(db, sess.token)
    assert auth.resolve_session(db, sess.token) is None


def test_expired_session_resolves_to_none(db) -> None:
    account = auth.signup(db, email="exp@example.com", password="Password123")
    sess = auth.issue_session(db, account)
    # Force the row to be expired.
    sess.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    assert auth.resolve_session(db, sess.token) is None


def test_resolve_session_none_for_missing_token(db) -> None:
    assert auth.resolve_session(db, None) is None
    assert auth.resolve_session(db, "no-such-token") is None
