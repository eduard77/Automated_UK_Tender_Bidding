"""Proactis login-failure diagnostic capture + admin endpoint — all offline.

A fake bridge stands in for the headless browser, serving a recognisable
"Access denied" block page so we can prove the snapshot carries everything
needed to tell a WAF block from a cookie wall from a missing login form:
current_url, page_title, last_status_code, per-selector presence, frames,
the trimmed page-text slice and a screenshot path. Secrets (the password,
cookie values) must never appear in the snapshot or the logs.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from tender_agent.api.deps import current_account
from tender_agent.main import app
from tender_agent.services.bridge_client import (
    BridgeError,
    MarkerAlternative,
    MarkerProbeResult,
)
from tender_agent.services.discovery.proactis_login_diagnostic import (
    TEXT_EXCERPT_CHARS,
    capture_login_state,
    run_login_diagnostic,
)
from tender_agent.services.portals.base import Credentials

_PASSWORD = "SuperSecret123!"
_CREDS = Credentials(
    username="ops@genera-systems.com",
    password=_PASSWORD,
    email="ops@genera-systems.com",
)

_BLOCK_PAGE_TEXT = (
    "Access denied. You don't have permission to access "
    '"https://procontract.due-north.com/Login/Index" on this server. '
    "Reference #18.5f4ed17.1749550000.1a2b3c"
)


class _BlockedBridge:
    """A cloud bridge whose page is a WAF block page: the navigation 'worked'
    (HTTP 403 rendered), but no login form exists, so every fill fails."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.page_text_value = _BLOCK_PAGE_TEXT
        self.status = {
            "exists": True,
            "current_url": "https://procontract.due-north.com/Login/Index",
            "authenticated_guess": False,
            "last_status_code": 403,
        }

    async def bridge_available(self) -> bool:
        return True

    async def open_session(self, slug, start_url=None) -> dict:
        self.calls.append(("open_session", slug))
        return {}

    async def close_session(self, slug) -> dict:
        self.calls.append(("close_session", slug))
        return {"closed": True}

    async def navigate(self, slug, url) -> dict:
        self.calls.append(("navigate", url))
        return {"current_url": url, "status_code": 403, "title": "Access Denied"}

    async def session_status(self, slug) -> dict:
        return dict(self.status)

    async def fill(self, slug, selector, value) -> dict:
        self.calls.append(("fill", selector))
        raise BridgeError(f"fill failed: timeout waiting for {selector}")

    async def click(self, slug, selector) -> dict:
        self.calls.append(("click", selector))
        raise BridgeError(f"click failed: timeout waiting for {selector}")

    async def element_exists(self, slug, selector) -> bool:
        return False

    async def page_text(self, slug) -> str:
        return self.page_text_value

    async def screenshot(self, slug, label="screenshot") -> dict:
        self.calls.append(("screenshot", label))
        return {"path": f"{label}.png", "size_bytes": 4321}


class _FrameAwareBlockedBridge(_BlockedBridge):
    """Same block page, but the bridge supports the rich frame-aware probe
    (the in-process client) — the snapshot should carry frames + per-frame
    selector presence from it."""

    async def probe_login_markers(self, slug, markers, *, timeout_ms=8000, poll_ms=250):
        return MarkerProbeResult(
            visible=False,
            page_title="Access Denied",
            current_url=self.status["current_url"],
            frames_searched=["main", "challenge-frame"],
            alternatives=[
                MarkerAlternative(
                    label=m["label"], selector=m["selector"], found=False
                )
                for m in markers
            ],
        )


# --- capture on the discovery failure branch --------------------------------


@pytest.mark.asyncio
async def test_login_then_get_status_logs_diagnostic_on_failure():
    """The discovery path itself must emit the diagnostic event when login
    fails — the live `login_blocked, current_url=null` case."""
    from tender_agent.services.discovery.proactis_discovery import (
        _login_then_get_status,
    )

    bridge = _BlockedBridge()
    with capture_logs() as logs:
        attempt, _url = await _login_then_get_status(bridge, _CREDS)
    assert attempt.status != "ok"
    diagnostics = [
        e for e in logs if e["event"] == "discovery.proactis.login_diagnostic"
    ]
    assert len(diagnostics) == 1
    diag = diagnostics[0]
    assert diag["current_url"] == "https://procontract.due-north.com/Login/Index"
    assert diag["last_status_code"] == 403
    assert "Access denied" in diag["page_text_excerpt"]
    assert diag["selectors_found"]["username_input"]["found"] is False
    assert diag["screenshot_path"].endswith(".png")


# --- snapshot contents -------------------------------------------------------


@pytest.mark.asyncio
async def test_run_login_diagnostic_returns_block_page_snapshot():
    bridge = _BlockedBridge()
    snapshot = await run_login_diagnostic(
        credentials=_CREDS, slug="procontract", bridge=bridge
    )
    assert snapshot["available"] is True
    assert snapshot["logged_in"] is False
    assert snapshot["outcome"] == "needs_login"
    assert snapshot["current_url"] == (
        "https://procontract.due-north.com/Login/Index"
    )
    assert snapshot["current_url_unreadable"] is False
    assert snapshot["last_status_code"] == 403
    # The text slice is what lets the operator recognise the failure mode.
    assert "Access denied" in snapshot["page_text_excerpt"]
    # Every login-flow control is reported, and none was found on a block page.
    for key in ("username_input", "password_input", "submit_button", "logged_in_marker"):
        assert snapshot["selectors_found"][key]["found"] is False
    assert snapshot["screenshot_path"] == "procontract-login-diagnostic.png"
    # Read-only contract: the probe closed its session.
    assert ("close_session", "procontract") in bridge.calls


@pytest.mark.asyncio
async def test_frame_aware_bridge_reports_frames_and_title():
    bridge = _FrameAwareBlockedBridge()
    snapshot = await run_login_diagnostic(
        credentials=_CREDS, slug="procontract", bridge=bridge
    )
    assert snapshot["frames"] == ["main", "challenge-frame"]
    assert snapshot["page_title"] == "Access Denied"
    assert snapshot["selectors_found"]["password_input"]["found"] is False


@pytest.mark.asyncio
async def test_unreadable_url_is_flagged_explicitly():
    """The exact live symptom: even the URL can't be read. The snapshot says
    so explicitly instead of leaving a silent null."""
    bridge = _BlockedBridge()
    bridge.status = {"exists": True, "current_url": None}
    snapshot = await run_login_diagnostic(
        credentials=_CREDS, slug="procontract", bridge=bridge
    )
    assert snapshot["current_url"] is None
    assert snapshot["current_url_unreadable"] is True


@pytest.mark.asyncio
async def test_page_text_excerpt_is_trimmed_and_tag_free():
    bridge = _BlockedBridge()
    bridge.page_text_value = "word " * 2000  # 10k chars of whitespace-y text
    snapshot = await run_login_diagnostic(
        credentials=_CREDS, slug="procontract", bridge=bridge
    )
    assert len(snapshot["page_text_excerpt"]) <= TEXT_EXCERPT_CHARS
    assert "  " not in snapshot["page_text_excerpt"]  # whitespace collapsed


@pytest.mark.asyncio
async def test_capture_survives_bridge_read_failures():
    """Diagnostics run on an already-failing path — a broken read is recorded,
    never raised."""

    class _Broken(_BlockedBridge):
        async def session_status(self, slug):
            raise BridgeError("session gone")

        async def page_text(self, slug):
            raise BridgeError("page gone")

        async def screenshot(self, slug, label="screenshot"):
            raise BridgeError("no screenshot")

    from tender_agent.services.discovery.proactis_login import LoginAttempt

    diag = await capture_login_state(
        _Broken(), "procontract", attempt=LoginAttempt(status="needs_login")
    )
    assert diag.current_url_unreadable is True
    assert "session_status" in (diag.capture_error or "")
    assert diag.page_text_excerpt == ""


# --- secrets stay out --------------------------------------------------------


@pytest.mark.asyncio
async def test_no_secrets_in_snapshot_or_logs():
    bridge = _FrameAwareBlockedBridge()
    with capture_logs() as logs:
        snapshot = await run_login_diagnostic(
            credentials=_CREDS, slug="procontract", bridge=bridge
        )
    dumped_snapshot = json.dumps(snapshot, default=str)
    dumped_logs = json.dumps(logs, default=str)
    assert _PASSWORD not in dumped_snapshot
    assert _PASSWORD not in dumped_logs
    # No cookie VALUES either — the snapshot may name the cookie-banner
    # selector (cookie_accept), but never reads or carries cookie contents.
    assert "set-cookie" not in dumped_snapshot.lower()
    assert "JSESSIONID" not in dumped_snapshot


# --- admin endpoint ----------------------------------------------------------


@pytest.fixture()
def anon_client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def auth_client():
    app.dependency_overrides[current_account] = lambda: object()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(current_account, None)


def test_endpoint_rejects_anonymous(anon_client: TestClient) -> None:
    resp = anon_client.post("/admin/portals/proactis/login-diagnostic")
    assert resp.status_code == 401


def test_endpoint_returns_snapshot_json(auth_client, monkeypatch) -> None:
    # Stored credential for the default (portal_id=348, user_id) pair, in an
    # offline store; the bridge factory is swapped for the blocked fake.
    from cryptography.fernet import Fernet

    from tender_agent.services import bridge_client as bridge_client_mod
    from tender_agent.services import credentials as creds_mod
    from tests._billing_fixtures import make_engine_and_session

    store = creds_mod.CredentialsStore(
        session_factory=make_engine_and_session()[1],
        encryption_key=Fernet.generate_key().decode(),
    )
    store.store_credentials(348, "eduard", _CREDS, platform_slug="proactis")
    monkeypatch.setattr(creds_mod, "_store", store)
    monkeypatch.setattr(
        bridge_client_mod, "make_bridge_client", lambda: _BlockedBridge()
    )

    resp = auth_client.post("/admin/portals/proactis/login-diagnostic")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["logged_in"] is False
    assert body["last_status_code"] == 403
    assert "Access denied" in body["page_text_excerpt"]
    assert body["selectors_found"]["username_input"]["found"] is False
    assert _PASSWORD not in resp.text


def test_endpoint_404_when_no_credentials_stored(auth_client, monkeypatch) -> None:
    from cryptography.fernet import Fernet

    from tender_agent.services import credentials as creds_mod
    from tests._billing_fixtures import make_engine_and_session

    empty_store = creds_mod.CredentialsStore(
        session_factory=make_engine_and_session()[1],
        encryption_key=Fernet.generate_key().decode(),
    )
    monkeypatch.setattr(creds_mod, "_store", empty_store)
    resp = auth_client.post("/admin/portals/proactis/login-diagnostic")
    assert resp.status_code == 404
    assert "no_proactis_credentials_stored" in resp.json()["detail"]
