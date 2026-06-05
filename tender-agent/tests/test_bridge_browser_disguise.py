"""Cloud bridge browser disguise — must present as ordinary desktop Chrome.

Delta's WAF 403s a browser that self-identifies as automated (default headless
UA contains "HeadlessChrome"; navigator.webdriver is set). These launch the
in-process cloud bridge EXACTLY as production does (headless Chromium, the real
`_launch` path) and assert the disguise holds: the user-agent has no "Headless",
and navigator.webdriver is gone. They read `navigator.*` from a locally-launched
page (no external service).

Skips cleanly where Chromium isn't installed, so CI without the Playwright image
stays green — run them inside the Playwright-bearing backend image for the real
check.
"""
from __future__ import annotations

import pytest

from tender_agent.services.bridge_in_process import (
    CLOUD_BROWSER_USER_AGENT,
    InProcessBridgeClient,
)

pytestmark = pytest.mark.asyncio


async def _launch_and_probe(slug: str, tmp_path, monkeypatch):
    """Launch the cloud bridge exactly as production (headless) and read the
    browser's navigator.userAgent + navigator.webdriver. Skips if Chromium is
    unavailable. Returns (client, ua, webdriver)."""
    from tender_agent.services import bridge_in_process as mod

    monkeypatch.setattr(mod.settings, "bridge_state_dir", str(tmp_path / "sessions"))
    monkeypatch.setattr(mod.settings, "bridge_download_dir", str(tmp_path / "dl"))
    # Production is headless — exercise the same path, not a headed override.
    monkeypatch.setenv("TENDER_AGENT_BRIDGE_HEADLESS", "true")

    client = InProcessBridgeClient()
    if not await client.bridge_available():
        pytest.skip("Playwright not installed")
    try:
        await client.open_session(slug, "about:blank")
    except Exception as exc:  # noqa: BLE001 — no Chromium binary in this env
        pytest.skip(f"Chromium unavailable: {exc}")
    page = client._manager.get(slug).page
    ua = await page.evaluate("() => navigator.userAgent")
    webdriver = await page.evaluate("() => navigator.webdriver")
    return client, ua, webdriver


async def test_cloud_browser_ua_has_no_headless(tmp_path, monkeypatch):
    client, ua, _ = await _launch_and_probe("disguise-ua", tmp_path, monkeypatch)
    try:
        assert "headless" not in ua.lower(), f"UA still advertises headless: {ua}"
        assert "Chrome/" in ua, ua
        assert ua == CLOUD_BROWSER_USER_AGENT
    finally:
        await client.close_session("disguise-ua")


async def test_cloud_browser_webdriver_hidden(tmp_path, monkeypatch):
    client, _, webdriver = await _launch_and_probe("disguise-wd", tmp_path, monkeypatch)
    try:
        # undefined (None over the bridge) or False — never True.
        assert webdriver in (None, False), f"navigator.webdriver = {webdriver!r}"
    finally:
        await client.close_session("disguise-wd")


async def test_cloud_ua_constant_is_not_headless():
    # Cheap guard that runs even without Chromium: the configured UA is a real
    # desktop Chrome, never a headless one.
    assert "headless" not in CLOUD_BROWSER_USER_AGENT.lower()
    assert "Chrome/" in CLOUD_BROWSER_USER_AGENT
    assert "Windows NT" in CLOUD_BROWSER_USER_AGENT
