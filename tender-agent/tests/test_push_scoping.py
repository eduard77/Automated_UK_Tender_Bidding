"""Per-user push dispatch — no cross-account leakage (offline, in-memory DB).

Proves: a per-account send reaches only that account's devices; an email-style
send for account A never reaches account B; a tender match for A's profile
notifies only A; an unknown/None target notifies nobody; legacy NULL-owner rows
are excluded from every dispatch; dead endpoints are still cleaned up and the
dispatch stays best-effort.
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pywebpush import WebPushException

from tender_agent.config import settings
from tender_agent.models import (
    Account,
    FilterProfile,
    PushSubscription,
    Tender,
)
from tender_agent.services import push
from tests._billing_fixtures import make_engine_and_session


@pytest.fixture()
def db():
    _, factory = make_engine_and_session()
    s = factory()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "vapid_public_key", "pub")
    monkeypatch.setattr(settings, "vapid_private_key", "priv")


@pytest.fixture()
def captured(monkeypatch):
    """Patch webpush to record the endpoints it was asked to send to."""
    endpoints: list[str] = []

    def _fake_webpush(*, subscription_info, data, vapid_private_key, vapid_claims):
        endpoints.append(subscription_info["endpoint"])

    monkeypatch.setattr(push, "webpush", _fake_webpush)
    return endpoints


def _account(db, email: str) -> Account:
    a = Account(email=email, password_hash="x", plan="free")
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


def _sub(db, *, account_id, endpoint, filter_profile_id=None) -> PushSubscription:
    s = PushSubscription(
        account_id=account_id,
        endpoint=endpoint,
        p256dh="p",
        auth="a",
        filter_profile_id=filter_profile_id,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _profile(db, name: str) -> FilterProfile:
    p = FilterProfile(name=name, enabled=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _tender(db) -> Tender:
    t = Tender(
        source_code="FTS",
        source_ref="r1",
        title="Cleaning services",
        buyer_name="Bristol CC",
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def test_send_to_account_targets_only_owner(db, captured) -> None:
    a = _account(db, "a@x.com")
    b = _account(db, "b@x.com")
    _sub(db, account_id=a.id, endpoint="epA")
    _sub(db, account_id=b.id, endpoint="epB")

    sent, failed = push.send_to_account(db, a.id, {"title": "t", "body": "b"})

    assert (sent, failed) == (1, 0)
    assert captured == ["epA"]  # never epB


def test_email_notification_does_not_leak_across_accounts(db, captured) -> None:
    # Mirrors the email path: two inboxes, an email for A notifies only A.
    from tender_agent.services.email.notify import notify_email_match

    a = _account(db, "a@x.com")
    b = _account(db, "b@x.com")
    _sub(db, account_id=a.id, endpoint="epA")
    _sub(db, account_id=b.id, endpoint="epB")
    tender = _tender(db)

    push_count = notify_email_match(
        db, tender, account_id=a.id, attachment_count=2, draft_ready=True
    )
    assert push_count == 1
    assert captured == ["epA"]


def test_none_target_notifies_nobody(db, captured) -> None:
    a = _account(db, "a@x.com")
    _sub(db, account_id=a.id, endpoint="epA")
    assert push.send_to_account(db, None, {"title": "t"}) == (0, 0)
    assert captured == []


def test_unknown_account_notifies_nobody(db, captured) -> None:
    a = _account(db, "a@x.com")
    _sub(db, account_id=a.id, endpoint="epA")
    assert push.send_to_account(db, 999_999, {"title": "t"}) == (0, 0)
    assert captured == []


def test_tender_match_is_per_user(db, captured) -> None:
    a = _account(db, "a@x.com")
    b = _account(db, "b@x.com")
    p = _profile(db, "A's profile")
    q = _profile(db, "B's profile")
    _sub(db, account_id=a.id, endpoint="epA", filter_profile_id=p.id)
    _sub(db, account_id=b.id, endpoint="epB", filter_profile_id=q.id)
    tender = _tender(db)

    push.send_match_notifications(db, tender, [p.id])

    assert captured == ["epA"]  # B subscribed to a different profile — not notified


def test_owned_catch_all_gets_matches_but_legacy_null_does_not(
    db, captured
) -> None:
    a = _account(db, "a@x.com")
    p = _profile(db, "profile")
    # Owned "all matches" subscription (filter_profile_id NULL) — should fire.
    _sub(db, account_id=a.id, endpoint="epOwnedCatchAll", filter_profile_id=None)
    # Legacy unowned row (account_id NULL) — must be excluded entirely.
    _sub(db, account_id=None, endpoint="epLegacy", filter_profile_id=None)
    tender = _tender(db)

    push.send_match_notifications(db, tender, [p.id])

    assert "epOwnedCatchAll" in captured
    assert "epLegacy" not in captured


def test_legacy_null_owner_excluded_from_account_send(db, captured) -> None:
    _sub(db, account_id=None, endpoint="epLegacy")
    # No account owns it, so no per-account send can ever reach it.
    assert push.send_to_account(db, None, {"title": "t"}) == (0, 0)
    assert captured == []


def test_dead_endpoint_is_cleaned_up(db, monkeypatch) -> None:
    a = _account(db, "a@x.com")
    sub = _sub(db, account_id=a.id, endpoint="epGone")

    def _gone(**kwargs):
        raise WebPushException(
            "gone", response=SimpleNamespace(status_code=410)
        )

    monkeypatch.setattr(push, "webpush", _gone)
    sent, failed = push.send_to_account(db, a.id, {"title": "t"})
    db.commit()

    assert (sent, failed) == (0, 1)
    assert db.get(PushSubscription, sub.id) is None  # 410 -> deleted


def test_dispatch_failure_is_swallowed(db, monkeypatch) -> None:
    a = _account(db, "a@x.com")
    _sub(db, account_id=a.id, endpoint="epA")
    tender = _tender(db)

    def _boom(*args, **kwargs):
        raise RuntimeError("dispatch exploded")

    monkeypatch.setattr(push, "_dispatch", _boom)
    # Best-effort: a dispatch error must not propagate to the caller.
    push.send_match_notifications(db, tender, [1])
