"""LIVE proof that the bridge works mechanically end-to-end.

Runs the real FastAPI app in-process with a real headless Chrome against the
public automation-practice site https://the-internet.herokuapp.com/login
(creds: tomsmith / SuperSecretPassword!). The test stands in for the human who
would normally type into the visible window — everything *downstream* of the
login (success detection, session persistence, authenticated read, authenticated
download) is exactly the real pipeline.

Marked `live`: needs network + a browser, so CI skips it. Run locally with:
    cd browser-bridge
    TENDER_AGENT_BRIDGE_HEADLESS=1 TENDER_AGENT_BRIDGE_TOKEN=test \
        python -m pytest tests/test_bridge_live.py -m live -s
"""
from __future__ import annotations

import os

import httpx
import pytest

LOGIN_URL = "https://the-internet.herokuapp.com/login"
SECURE_PATTERN = r"/secure"
TOKEN = os.environ.get("TENDER_AGENT_BRIDGE_TOKEN", "test")
HEADERS = {"X-Bridge-Token": TOKEN}


pytestmark = pytest.mark.live


@pytest.fixture()
def app_client():
    os.environ.setdefault("TENDER_AGENT_BRIDGE_TOKEN", "test")
    os.environ["TENDER_AGENT_BRIDGE_HEADLESS"] = "1"
    from bridge.server import app

    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://bridge.test")


@pytest.mark.asyncio
async def test_full_bridge_pipeline(app_client, capsys):
    from bridge.server import manager

    client = app_client
    try:
        # 1. Health (no token).
        h = await client.get("/health")
        assert h.status_code == 200
        print("health:", h.json())

        # 2. Open a visible session at the login page.
        r = await client.post(
            "/session/open",
            json={"platform_slug": "practice", "start_url": LOGIN_URL},
            headers=HEADERS,
        )
        assert r.status_code == 200, r.text
        print("open:", r.json())

        # 3. Stand in for the human: type creds + submit in the page.
        session = manager.get("practice")
        assert session is not None
        await session.page.fill("#username", "tomsmith")
        await session.page.fill("#password", "SuperSecretPassword!")
        await session.page.click("button[type=submit]")

        # 4. Login-success detection (URL becomes /secure).
        w = await client.post(
            "/session/practice/wait-for-login",
            json={"success_url_pattern": SECURE_PATTERN, "timeout_seconds": 20},
            headers=HEADERS,
        )
        assert w.status_code == 200, w.text
        print("wait-for-login:", w.json())
        assert w.json()["status"] == "logged_in"

        # 5. Authenticated page read.
        t = await client.get("/session/practice/page-text", headers=HEADERS)
        assert t.status_code == 200
        assert "secure area" in t.json()["text"].lower()
        print("secure page text ok")

        # 6. Session persistence: close, reopen, still authenticated.
        await client.post("/session/practice/close", headers=HEADERS)
        r2 = await client.post(
            "/session/open",
            json={
                "platform_slug": "practice",
                "start_url": "https://the-internet.herokuapp.com/secure",
            },
            headers=HEADERS,
        )
        assert r2.status_code == 200
        print("reopen:", r2.json())
        t2 = await client.get("/session/practice/page-text", headers=HEADERS)
        assert "secure area" in t2.json()["text"].lower(), (
            "session did not persist — re-login required"
        )
        print("session persisted: /secure reachable without re-login")

        # 7. Authenticated download of a static asset.
        d = await client.post(
            "/session/practice/download",
            json={
                "url": "https://the-internet.herokuapp.com/img/avatar-blank.jpg",
                "dest_filename": "avatar.jpg",
            },
            headers=HEADERS,
        )
        assert d.status_code == 200, d.text
        meta = d.json()
        print("download:", {k: v for k, v in meta.items() if k != "b64"})
        assert meta["size_bytes"] > 0

        from bridge import config

        landed = config.download_dir() / meta["path"]
        assert landed.is_file()
        assert landed.stat().st_size == meta["size_bytes"]
        print("LIVE BRIDGE PIPELINE: PASS")
    finally:
        await client.post("/session/practice/close", headers=HEADERS)
        await client.aclose()


@pytest.mark.asyncio
async def test_token_required(app_client):
    client = app_client
    try:
        r = await client.post(
            "/session/open",
            json={"platform_slug": "practice", "start_url": LOGIN_URL},
            headers={"X-Bridge-Token": "wrong"},
        )
        assert r.status_code == 401
    finally:
        await client.aclose()
