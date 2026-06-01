"""Signup / login / session services.

Sessions are server-side: we generate a random token, persist
(account_id, token, expires_at) in account_sessions, and hand the raw token to
the client as an httpOnly cookie. Revocation = DELETE. Tokens are NOT JWTs.

Email is normalised (lowercased + trimmed) on the way in so `Foo@Bar.com` and
`foo@bar.com` can't both sign up.
"""
from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.config import settings
from tender_agent.models import Account, AccountSession
from tender_agent.services.accounts.passwords import (
    hash_password,
    verify_password,
)

logger = structlog.get_logger(__name__)


class AuthError(Exception):
    """Raised by signup/login on a known failure mode. The API layer maps to
    400/401 — we don't surface the reason to the caller (timing-safe-ish)."""


def normalise_email(email: str) -> str:
    return email.strip().lower()


def signup(db: Session, *, email: str, password: str) -> Account:
    """Create a free-tier account. Idempotent on email — re-using an existing
    email raises AuthError("email_taken") so the API can return a single
    "credentials invalid"-shaped error instead of leaking enumeration."""
    norm = normalise_email(email)
    if not norm or "@" not in norm:
        raise AuthError("invalid_email")
    if not password or len(password) < 8:
        raise AuthError("weak_password")

    existing = db.execute(
        select(Account).where(Account.email == norm)
    ).scalar_one_or_none()
    if existing is not None:
        raise AuthError("email_taken")

    account = Account(
        email=norm,
        password_hash=hash_password(password),
        plan="free",
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    logger.info("accounts.signup", account_id=account.id)
    return account


def login(db: Session, *, email: str, password: str) -> Account:
    """Verify creds and return the account. Always runs bcrypt verify (even
    on a missing-account miss) so the response time is uniform — no oracle."""
    norm = normalise_email(email)
    account = db.execute(
        select(Account).where(Account.email == norm)
    ).scalar_one_or_none()
    # Run a dummy verify so the timing of "wrong email" looks like "wrong
    # password". The hash here is a constant from an empty-string bcrypt.
    if account is None:
        verify_password(password, _DUMMY_HASH)
        raise AuthError("invalid_credentials")
    if not verify_password(password, account.password_hash):
        raise AuthError("invalid_credentials")
    return account


# Pre-computed bcrypt hash of "x" — used only as a constant-time dummy when
# the account lookup misses. Value isn't sensitive; it's just a real bcrypt
# string so verify_password takes the bcrypt branch and not the empty-input
# short-circuit.
_DUMMY_HASH = "$2b$12$pUjE6Pr4ufkixz5HEpCnZ.5sjU8tBSvFhpoQOnxnIyT0jJa0HVRDe"


def issue_session(db: Session, account: Account) -> AccountSession:
    """Mint a new session token for the account and persist it. The raw token
    string is what goes into the httpOnly cookie."""
    token = secrets.token_urlsafe(48)
    now = datetime.now(UTC)
    sess = AccountSession(
        account_id=account.id,
        token=token,
        created_at=now,
        expires_at=now + timedelta(days=settings.session_lifetime_days),
    )
    db.add(sess)
    db.commit()
    db.refresh(sess)
    return sess


def resolve_session(db: Session, token: str | None) -> Account | None:
    """Look up an account by an opaque session token. Returns None for
    missing/expired/invalid — callers treat that as anonymous."""
    if not token:
        return None
    sess = db.execute(
        select(AccountSession).where(AccountSession.token == token)
    ).scalar_one_or_none()
    if sess is None:
        return None
    expires_at = sess.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC):
        return None
    return db.get(Account, sess.account_id)


def revoke_session(db: Session, token: str) -> None:
    sess = db.execute(
        select(AccountSession).where(AccountSession.token == token)
    ).scalar_one_or_none()
    if sess is None:
        return
    db.delete(sess)
    db.commit()
