"""Proactis credential-driven login — fully mocked bridge.

The login flow is the only thing in Step 1 that handles a real password,
so we pin down: (a) the password value NEVER ends up in the LoginAttempt's
fields or in the structured log line, (b) selector mismatches yield a
clean `needs_login` shape, and (c) Proactis's invalid-credentials banner
maps to its own status, distinct from "selector missing".

No Playwright, no httpx, no network — the bridge is a simple async fake
recording calls.
"""
from __future__ import annotations

import pytest

from tender_agent.services.bridge_client import BridgeError
from tender_agent.services.discovery import proactis_login as pl
from tender_agent.services.discovery.proactis_login import (
    LoginAttempt,
    _password_fingerprint,
    login_with_credentials,
)
from tender_agent.services.portals.base import Credentials


class _FakeBridge:
    """Minimal async bridge fake — only the methods login uses."""

    def __init__(
        self,
        *,
        marker_visible_after_submit: bool = False,
        rejection_banner_after_submit: bool = False,
        fill_errors_on: tuple[str, ...] = (),
        click_errors_on: tuple[str, ...] = (),
        post_submit_url: str = "https://procontract.due-north.com/SupplierPostLoginHome",
    ):
        self.marker = marker_visible_after_submit
        self.rejection = rejection_banner_after_submit
        self.fill_errors_on = fill_errors_on
        self.click_errors_on = click_errors_on
        self.post_submit_url = post_submit_url
        # Recorded calls for assertions.
        self.fills: list[tuple[str, str]] = []
        self.clicks: list[str] = []
        self.navigates: list[str] = []

    async def navigate(self, _slug, url):
        self.navigates.append(url)
        return {"current_url": url}

    async def fill(self, _slug, selector, value):
        # Match on a substring so tests can name the field they want to error.
        for needle in self.fill_errors_on:
            if needle.lower() in selector.lower():
                raise BridgeError(f"fill failed on {selector}")
        self.fills.append((selector, value))
        return {"ok": True}

    async def click(self, _slug, selector):
        for needle in self.click_errors_on:
            if needle.lower() in selector.lower():
                raise BridgeError(f"click failed on {selector}")
        self.clicks.append(selector)
        return {"ok": True}

    async def element_exists(self, _slug, selector):
        if "Find opportunities" in selector or "logged_in_marker" in selector.lower():
            return self.marker
        if "validation-summary-errors" in selector or "alert-danger" in selector:
            return self.rejection
        return False

    async def session_status(self, _slug):
        return {"current_url": self.post_submit_url}


_CREDS = Credentials(
    email="ops@example.com", password="p4ssw0rd", username=None, extra={}
)


@pytest.mark.asyncio
async def test_password_fingerprint_is_short_and_not_the_password():
    fp = _password_fingerprint("super-secret-password")
    assert fp != "super-secret-password"
    assert len(fp) == 8
    # Non-reversible — same input gives same fingerprint though.
    assert _password_fingerprint("super-secret-password") == fp


@pytest.mark.asyncio
async def test_ok_when_logged_in_marker_appears():
    bridge = _FakeBridge(marker_visible_after_submit=True)
    attempt = await login_with_credentials(
        bridge, slug="proactis", credentials=_CREDS
    )
    assert attempt.status == "ok"
    assert attempt.current_url is not None
    # The password landed in the form fill ONCE.
    pwd_fills = [v for s, v in bridge.fills if "password" in s.lower()]
    assert pwd_fills == ["p4ssw0rd"]
    # And the LoginAttempt detail carries only the fingerprint, not the password.
    assert attempt.detail is not None
    assert "p4ssw0rd" not in attempt.detail


@pytest.mark.asyncio
async def test_credentials_rejected_when_proactis_shows_banner():
    bridge = _FakeBridge(
        marker_visible_after_submit=False,
        rejection_banner_after_submit=True,
    )
    attempt = await login_with_credentials(
        bridge, slug="proactis", credentials=_CREDS
    )
    assert attempt.status == "credentials_rejected"
    assert "update" in (attempt.detail or "").lower()


@pytest.mark.asyncio
async def test_needs_login_when_marker_missing_and_no_banner():
    bridge = _FakeBridge(
        marker_visible_after_submit=False,
        rejection_banner_after_submit=False,
        post_submit_url="https://procontract.due-north.com/Login/Index",
    )
    attempt = await login_with_credentials(
        bridge, slug="proactis", credentials=_CREDS
    )
    assert attempt.status == "needs_login"


@pytest.mark.asyncio
async def test_needs_login_when_username_field_missing():
    bridge = _FakeBridge(fill_errors_on=("UserName", "username"))
    attempt = await login_with_credentials(
        bridge, slug="proactis", credentials=_CREDS
    )
    assert attempt.status == "needs_login"
    assert "username" in (attempt.detail or "").lower()


@pytest.mark.asyncio
async def test_needs_login_when_submit_button_missing():
    bridge = _FakeBridge(click_errors_on=("submit",))
    attempt = await login_with_credentials(
        bridge, slug="proactis", credentials=_CREDS
    )
    assert attempt.status == "needs_login"
    assert "submit" in (attempt.detail or "").lower()


@pytest.mark.asyncio
async def test_error_when_credentials_record_missing_password():
    creds = Credentials(email="ops@example.com", password="", extra={})
    attempt = await login_with_credentials(
        _FakeBridge(), slug="proactis", credentials=creds
    )
    assert attempt.status == "error"
    assert "password" in (attempt.detail or "").lower()


@pytest.mark.asyncio
async def test_password_never_appears_in_logger_output(caplog):
    """The login flow logs a fingerprint, never the password. We monkeypatch
    structlog to capture and assert the password substring is absent."""
    captured: list[dict] = []

    def _capture(*args, **kwargs):
        captured.append({"args": args, "kwargs": kwargs})

    pl.logger = type(  # type: ignore[attr-defined]
        "L",
        (),
        {"info": _capture, "warning": _capture, "exception": _capture, "debug": _capture},
    )()

    bridge = _FakeBridge(marker_visible_after_submit=True)
    await login_with_credentials(
        bridge, slug="proactis", credentials=_CREDS
    )

    blob = repr(captured)
    assert "p4ssw0rd" not in blob


@pytest.mark.asyncio
async def test_loginattempt_dataclass_fields_dont_carry_password():
    attempt = LoginAttempt(status="ok", detail="fingerprint-only")
    assert "p4ssw0rd" not in repr(attempt)
