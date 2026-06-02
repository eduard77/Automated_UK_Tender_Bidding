"""In-process Playwright driver — factory wiring + the bits that don't need
a real browser. The Delta-specific row-click flow is exercised by
test_delta_adapter.py via a fake bridge satisfying the same Protocol; we don't
launch real Chromium in CI.
"""
from __future__ import annotations

import pytest

from tender_agent.services import bridge_in_process as bip
from tender_agent.services.bridge_client import (
    BridgeError,
    HttpBridgeClient,
    make_bridge_client,
)


def test_factory_returns_in_process_when_bridge_url_empty(monkeypatch):
    monkeypatch.setattr("tender_agent.services.bridge_client.settings.bridge_url", "")
    client = make_bridge_client()
    assert isinstance(client, bip.InProcessBridgeClient)


def test_factory_returns_http_when_bridge_url_set(monkeypatch):
    monkeypatch.setattr(
        "tender_agent.services.bridge_client.settings.bridge_url",
        "http://bridge.test:8765",
    )
    client = make_bridge_client()
    assert isinstance(client, HttpBridgeClient)


def test_factory_caller_base_url_wins_over_settings(monkeypatch):
    monkeypatch.setattr("tender_agent.services.bridge_client.settings.bridge_url", "")
    client = make_bridge_client(base_url="http://override:8765")
    assert isinstance(client, HttpBridgeClient)


def test_in_process_safe_name_redacts_path_and_unsafe_chars():
    assert bip._safe_name("foo/bar..//baz??.pdf") == "baz__.pdf"
    assert bip._safe_name("..//etc/passwd") == "passwd"


def test_in_process_authenticated_guess_login_url_returns_false():
    assert bip._authenticated_guess("https://x.test/login", "<html></html>") is False


def test_in_process_authenticated_guess_password_field_returns_false():
    assert (
        bip._authenticated_guess(
            "https://x.test/secure", '<input type="password">'
        )
        is False
    )


def test_in_process_authenticated_guess_plain_secure_page_returns_true():
    assert (
        bip._authenticated_guess(
            "https://x.test/secure", "<h1>Dashboard</h1>"
        )
        is True
    )


def test_in_process_state_dir_per_slug_sanitised(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tender_agent.services.bridge_in_process.settings.bridge_state_dir",
        str(tmp_path / "sessions"),
    )
    d = bip._state_dir_for_slug("delta../weird/slug")
    assert d.exists()
    # Path-traversal characters get scrubbed; the dir name is alnum+-+_.
    assert all(c.isalnum() or c in "-_" for c in d.name)
    assert d.parent == (tmp_path / "sessions")


@pytest.mark.asyncio
async def test_in_process_bridge_available_reports_playwright_install_state():
    """bridge_available is the fast preflight that flags 'image lacks the
    Playwright runtime'. Mirror the actual import probe behaviour: True iff
    `import playwright` succeeds."""
    client = bip.InProcessBridgeClient()
    expected: bool
    try:
        import playwright  # noqa: F401

        expected = True
    except Exception:
        expected = False
    assert await client.bridge_available() is expected


@pytest.mark.asyncio
async def test_in_process_requires_open_session():
    client = bip.InProcessBridgeClient()
    with pytest.raises(BridgeError):
        client._require("never-opened")


@pytest.mark.asyncio
async def test_in_process_session_status_returns_no_session():
    client = bip.InProcessBridgeClient()
    status = await client.session_status("not-opened")
    assert status == {
        "exists": False,
        "current_url": None,
        "authenticated_guess": False,
    }


def test_in_process_download_dir_matches_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "tender_agent.services.bridge_in_process.settings.bridge_download_dir",
        str(tmp_path / "downloads"),
    )
    assert bip._download_dir() == tmp_path / "downloads"


def test_headless_default_true():
    # No env var set — defaults to True in container.
    import os

    os.environ.pop("TENDER_AGENT_BRIDGE_HEADLESS", None)
    assert bip._headless() is True


def test_headless_env_off(monkeypatch):
    monkeypatch.setenv("TENDER_AGENT_BRIDGE_HEADLESS", "false")
    assert bip._headless() is False
