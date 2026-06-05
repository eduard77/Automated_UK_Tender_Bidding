"""Login model B (Delta cloud stage 3): admin session upload/status/test.

Covers: auth gating (401 for anonymous), that a valid storage_state lands in the
slug dir in the loadable form, malformed rejection, status reporting without
leaking cookie values, and the `test_session` probe running `is_authenticated`
headless without changing state (no click / register).

No Playwright/Chromium is launched: the upload/status routes are pure
filesystem, and the probe is exercised against an injected fake bridge + the
REAL Delta adapter, so we verify the genuine probe path offline.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tender_agent.config import settings
from tender_agent.db import engine, get_db
from tender_agent.main import app
from tender_agent.services.bridge_client import RenderedPage
from tender_agent.services.portals import delta_session
from tests._auth_helpers import authenticate_unlimited

VALID_STATE = {
    "cookies": [
        {
            "name": "JSESSIONID",
            "value": "fake-session-token",
            "domain": ".delta-esourcing.com",
            "path": "/",
            "expires": -1,
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        }
    ],
    "origins": [
        {
            "origin": "https://www.delta-esourcing.com",
            "localStorage": [{"name": "k", "value": "v"}],
        }
    ],
}


@pytest.fixture()
def session() -> Session:
    connection = engine.connect()
    outer = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        connection.close()


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    """Point bridge_state_dir at a temp dir so uploads never touch the real
    /app/data path. `_state_dir_for_slug` reads this attribute at call time."""
    monkeypatch.setattr(settings, "bridge_state_dir", str(tmp_path))
    return tmp_path


@pytest.fixture()
def anon_client(session, state_dir) -> TestClient:
    def override():
        yield session

    app.dependency_overrides[get_db] = override
    tc = TestClient(app)
    try:
        yield tc
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture()
def auth_client(session, state_dir) -> TestClient:
    def override():
        yield session

    app.dependency_overrides[get_db] = override
    tc = TestClient(app)
    try:
        authenticate_unlimited(tc)
        yield tc
    finally:
        app.dependency_overrides.pop(get_db, None)


# --- auth gating -----------------------------------------------------------


def test_endpoints_reject_anonymous(anon_client):
    assert (
        anon_client.post("/admin/portals/delta/session", json=VALID_STATE).status_code
        == 401
    )
    assert anon_client.get("/admin/portals/delta/session/status").status_code == 401
    # The probe endpoint must be rejected at the dependency BEFORE any browser
    # launch is attempted.
    assert anon_client.post("/admin/portals/delta/session/test").status_code == 401


# --- upload ----------------------------------------------------------------


def test_upload_writes_storage_state(auth_client, state_dir):
    r = auth_client.post("/admin/portals/delta/session", json=VALID_STATE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["slug"] == delta_session.DELTA_SLUG
    assert body["updated_at"]

    target = state_dir / delta_session.DELTA_SLUG / "storage_state.json"
    assert target.exists(), "storage_state must land in the slug's context dir"
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written == VALID_STATE  # loadable form, structurally faithful


def test_upload_overwrites_existing(auth_client, state_dir):
    auth_client.post("/admin/portals/delta/session", json=VALID_STATE)
    second = {"cookies": [], "origins": []}
    r = auth_client.post("/admin/portals/delta/session", json=second)
    assert r.status_code == 200, r.text
    target = state_dir / delta_session.DELTA_SLUG / "storage_state.json"
    assert json.loads(target.read_text(encoding="utf-8")) == second


def test_upload_rejects_malformed(auth_client):
    # not JSON
    assert (
        auth_client.post(
            "/admin/portals/delta/session",
            content=b"not json",
            headers={"content-type": "application/json"},
        ).status_code
        == 400
    )
    # JSON object but missing the cookies/origins lists
    assert (
        auth_client.post(
            "/admin/portals/delta/session", json={"foo": "bar"}
        ).status_code
        == 400
    )
    # cookies present but a cookie is malformed (no value/domain)
    assert (
        auth_client.post(
            "/admin/portals/delta/session",
            json={"cookies": [{"name": "x"}], "origins": []},
        ).status_code
        == 400
    )
    # empty body
    assert (
        auth_client.post(
            "/admin/portals/delta/session",
            content=b"",
            headers={"content-type": "application/json"},
        ).status_code
        == 400
    )


# --- status ----------------------------------------------------------------


def test_status_reports_presence_without_values(auth_client, state_dir):
    before = auth_client.get("/admin/portals/delta/session/status").json()
    assert before["present"] is False
    assert before["cookie_count"] == 0

    auth_client.post("/admin/portals/delta/session", json=VALID_STATE)

    after = auth_client.get("/admin/portals/delta/session/status").json()
    assert after["present"] is True
    assert after["cookie_count"] == 1
    assert after["has_delta_cookies"] is True
    assert after["updated_at"]
    # The secret cookie value must never appear in the status payload.
    assert "fake-session-token" not in json.dumps(after)


# --- test-session probe (offline, via injected fake bridge) ----------------


class _FakeBridge:
    """Minimal bridge that satisfies the methods `is_authenticated` calls.
    Records every call so the test can assert no click/register happened."""

    def __init__(self, *, logged_in: bool) -> None:
        self._logged_in = logged_in
        self.calls: list = []
        self._rm = "https://www.delta-esourcing.com/respond/responseManager.html"

    async def bridge_available(self) -> bool:
        self.calls.append("bridge_available")
        return True

    async def open_session(self, slug, start_url):
        self.calls.append(("open_session", slug))
        return {"current_url": start_url, "session_id": slug}

    async def navigate(self, slug, url):
        self.calls.append(("navigate", url))
        return {"current_url": url}

    async def session_status(self, slug):
        self.calls.append(("session_status", slug))
        return {"current_url": self._rm}

    async def rendered_html(self, slug, *, wait_for_selector=None,
                            wait_for_text=None, timeout_ms=15000):
        self.calls.append(("rendered_html", wait_for_selector))
        return RenderedPage(
            html="<html></html>",
            wait_satisfied=self._logged_in,
            current_url=self._rm,
        )

    async def close_session(self, slug):
        self.calls.append(("close_session", slug))
        return {"closed": True}

    # State-changing methods that the probe MUST NOT call.
    async def click(self, *a, **k):
        raise AssertionError("probe must not click")

    async def click_download_in_row(self, *a, **k):
        raise AssertionError("probe must not download")

    async def select_option(self, *a, **k):
        raise AssertionError("probe must not select_option")


async def test_test_session_reports_logged_in_without_state_change():
    fake = _FakeBridge(logged_in=True)
    res = await delta_session.test_session(bridge=fake)
    assert res["available"] is True
    assert res["logged_in"] is True
    # Backward-compatible richer diagnostics are present (added, not removed).
    for key in ("current_url", "title", "redirected_to_login",
                "frames_checked", "markers"):
        assert key in res
    # Session was opened and cleanly closed; no state-changing calls happened.
    assert ("close_session", delta_session.DELTA_SLUG) in fake.calls
    assert "click" not in [c if isinstance(c, str) else c[0] for c in fake.calls]


class _RichFakeBridge(_FakeBridge):
    """A bridge exposing the frame-aware logged_in_marker_report so test_session
    surfaces per-marker diagnostics (which alt was found, in which frame)."""

    async def logged_in_marker_report(self, slug, selectors, timeout_ms=8000):
        self.calls.append(("logged_in_marker_report", len(selectors)))
        return {
            "title": "Activity Centre | Delta",
            "current_url": "https://www.delta-esourcing.com/delta/mainMenu.html",
            "frames_checked": 2,
            "any_visible": True,
            "markers": [
                {"selector": selectors[0], "found": True, "frame": "main",
                 "matches": 1, "error": None},
            ],
        }


async def test_test_session_surfaces_marker_diagnostics():
    fake = _RichFakeBridge(logged_in=True)
    res = await delta_session.test_session(bridge=fake)
    assert res["logged_in"] is True
    assert res["title"] == "Activity Centre | Delta"
    assert res["frames_checked"] == 2
    assert res["markers"][0]["found"] is True
    # No cookie values anywhere in the payload.
    assert "fake-session-token" not in json.dumps(res)


async def test_test_session_reports_not_logged_in():
    fake = _FakeBridge(logged_in=False)
    res = await delta_session.test_session(bridge=fake)
    assert res["available"] is True
    assert res["logged_in"] is False


async def test_test_session_handles_unavailable_bridge():
    class _Down(_FakeBridge):
        async def bridge_available(self) -> bool:
            return False

    res = await delta_session.test_session(bridge=_Down(logged_in=False))
    assert res["available"] is False
    assert res["logged_in"] is False


class _FrameBridge(_FakeBridge):
    """A bridge that supports the frame-aware probe (like the in-process cloud
    bridge), so /session/test surfaces the richer diagnostics. Returns a marker
    visible inside a frame."""

    async def probe_login_markers(self, slug, markers, *, timeout_ms=8000,
                                  poll_ms=250):
        from tender_agent.services.bridge_client import (
            MarkerAlternative,
            MarkerProbeResult,
        )

        self.calls.append(("probe_login_markers", len(markers)))
        alts = [
            MarkerAlternative(
                label=m["label"], selector=m["selector"],
                found=(i == 0), visible=(i == 0 and self._logged_in),
                frame="menuFrame" if i == 0 else None,
            )
            for i, m in enumerate(markers)
        ]
        return MarkerProbeResult(
            visible=self._logged_in,
            page_title="Activity Centre | Delta",
            current_url=self._rm,
            frames_searched=["main", "menuFrame"],
            alternatives=alts,
        )


async def test_test_session_surfaces_rich_diagnostics():
    fake = _FrameBridge(logged_in=True)
    res = await delta_session.test_session(bridge=fake)
    assert res["available"] is True
    assert res["logged_in"] is True
    # Backward-compatible additions: title + per-marker detail + frames searched.
    assert res["page_title"] == "Activity Centre | Delta"
    assert res["detection"] == "frames"
    assert res["frames_searched"] == ["main", "menuFrame"]
    first = res["markers"][0]
    assert first["found"] is True and first["visible"] is True
    assert first["frame"] == "menuFrame"
    assert res["marker_error"] is None
    # Still non-destructive: opened + closed, never clicked.
    assert ("close_session", delta_session.DELTA_SLUG) in fake.calls
    assert ("probe_login_markers", len(res["markers"])) in fake.calls
    # No cookie values anywhere in the payload.
    assert "fake-session-token" not in json.dumps(res)


async def test_test_session_diagnostics_explain_false():
    fake = _FrameBridge(logged_in=False)
    res = await delta_session.test_session(bridge=fake)
    assert res["logged_in"] is False
    assert res["detection"] == "frames"
    assert all(m["visible"] is False for m in res["markers"])
