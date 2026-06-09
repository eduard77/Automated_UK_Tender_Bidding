"""Unit tests for the push dispatch service (payload + guards).

The DB-backed per-user scoping is covered in test_push_scoping.py. Here we test
the pure bits: payload shape, the "not configured" guard, and the safe-failure
contract that a None target account notifies nobody (never everybody).
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from tender_agent.config import settings
from tender_agent.models import Tender
from tender_agent.services import push


def _tender() -> Tender:
    t = Tender(
        source_code="FTS",
        source_ref="ref-1",
        title="Cleaning services",
        buyer_name="Bristol City Council",
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )
    t.id = 42
    return t


def test_build_tender_match_payload_shape() -> None:
    payload = push._build_tender_match_payload(_tender())
    assert payload["title"] == "New tender match"
    assert "Cleaning services" in payload["body"]
    assert "Bristol City Council" in payload["body"]
    assert payload["url"].endswith("/tenders/42")
    assert payload["tag"] == "match-42"


def test_build_payload_handles_missing_buyer() -> None:
    t = _tender()
    t.buyer_name = None
    payload = push._build_tender_match_payload(t)
    assert payload["body"] == "Cleaning services"


def test_push_configured_reflects_settings() -> None:
    with patch.object(settings, "vapid_public_key", ""), patch.object(
        settings, "vapid_private_key", ""
    ):
        assert push.push_configured() is False
    with patch.object(settings, "vapid_public_key", "pub"), patch.object(
        settings, "vapid_private_key", "priv"
    ):
        assert push.push_configured() is True


def test_send_to_account_noop_when_unconfigured() -> None:
    with patch.object(settings, "vapid_public_key", ""), patch.object(
        settings, "vapid_private_key", ""
    ):
        # No DB touch — push_configured() short-circuits before any query.
        assert push.send_to_account(db=None, account_id=1, payload={}) == (0, 0)


def test_send_to_account_none_target_notifies_nobody() -> None:
    # Configured, but no target account => nobody (and no DB query).
    with patch.object(settings, "vapid_public_key", "pub"), patch.object(
        settings, "vapid_private_key", "priv"
    ):
        assert push.send_to_account(db=None, account_id=None, payload={}) == (0, 0)


def test_send_match_notifications_empty_profiles_is_noop() -> None:
    # Returns without touching DB or dispatching.
    with patch("tender_agent.services.push._dispatch") as dispatch:
        push.send_match_notifications(db=None, tender=_tender(), matched_profile_ids=[])
        dispatch.assert_not_called()


def test_no_catch_all_dispatch_function_remains() -> None:
    # The old catch-all entry point is gone; nothing can blast all subscribers.
    assert not hasattr(push, "send_to_subscribers")
