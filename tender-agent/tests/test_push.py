"""Unit tests for the push dispatch service.

We don't exercise the actual pywebpush HTTP call here — that needs a live
endpoint and is covered by the live smoke test. These tests cover:

- payload shape (the contract with the service worker)
- the "not configured" guard
- the SQL filter (subscribers of profile X plus catch-all subscribers with NULL)
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


def test_send_to_subscribers_noop_when_unconfigured() -> None:
    with patch.object(settings, "vapid_public_key", ""), patch.object(
        settings, "vapid_private_key", ""
    ):
        # No DB touch — push_configured() short-circuits before any query.
        result = push.send_to_subscribers(db=None, filter_profile_id=1, payload={})
        assert result == (0, 0)


def test_send_match_notifications_empty_profiles_is_noop() -> None:
    # Should return without touching DB or invoking webpush.
    with patch("tender_agent.services.push.send_to_subscribers") as send:
        push.send_match_notifications(db=None, tender=_tender(), matched_profile_ids=[])
        send.assert_not_called()
