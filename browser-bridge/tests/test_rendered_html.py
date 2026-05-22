"""Unit tests for the /rendered-html endpoint's wait logic — no real browser.

The endpoint must return the RENDERED DOM (document.documentElement.outerHTML)
after waiting for a marker (selector or text) that proves the page's JS-injected
content has appeared. These inject a fake Playwright page into the session
manager so the wait-for-text / wait-for-selector / graceful-timeout paths are
covered without a browser or network.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import httpx
import pytest

os.environ.setdefault("TENDER_AGENT_BRIDGE_TOKEN", "test")
TOKEN = os.environ["TENDER_AGENT_BRIDGE_TOKEN"]
HEADERS = {"X-Bridge-Token": TOKEN}


class FakePage:
    """A stand-in Playwright page.

    `contents` is the sequence content() returns on successive polls (so a page
    can "render" its rows only after a few polls). evaluate() returns the final
    rendered outerHTML. wait_for_selector succeeds unless selector_ok is False.
    """

    def __init__(
        self,
        *,
        contents: list[str],
        rendered: str,
        url: str = "https://www.delta-esourcing.com/delta/x",
        selector_ok: bool = True,
    ) -> None:
        self._contents = list(contents)
        self._rendered = rendered
        self.url = url
        self._selector_ok = selector_ok
        self.selector_waits = 0

    async def content(self) -> str:
        if len(self._contents) > 1:
            return self._contents.pop(0)
        return self._contents[0] if self._contents else ""

    async def evaluate(self, _script: str) -> str:
        return self._rendered

    async def wait_for_selector(self, _selector: str, timeout: int | None = None):
        self.selector_waits += 1
        if not self._selector_ok:
            raise TimeoutError("selector not found")
        return object()


@pytest.fixture()
def client_and_manager():
    from bridge.server import app, manager

    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://bridge.test")
    return client, manager


async def _post_rendered(client, manager, page, body):
    manager._sessions["delta"] = SimpleNamespace(page=page)
    try:
        return await client.post(
            "/session/delta/rendered-html", json=body, headers=HEADERS
        )
    finally:
        manager._sessions.pop("delta", None)
        await client.aclose()


@pytest.mark.asyncio
async def test_wait_for_text_satisfied_after_render(client_and_manager):
    client, manager = client_and_manager
    shell = "<html><bip-table></bip-table></html>"
    rendered = "<html><table>downloadDocument link</table></html>"
    # Shell on the first two polls, then the rendered content with the marker.
    page = FakePage(contents=[shell, shell, rendered], rendered=rendered)
    resp = await _post_rendered(
        client, manager, page,
        {"wait_for_text": "downloadDocument", "timeout_ms": 15000},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["wait_satisfied"] is True
    # Returns the RENDERED outerHTML (from evaluate), not the initial shell.
    assert data["html"] == rendered


@pytest.mark.asyncio
async def test_wait_for_text_times_out_gracefully(client_and_manager):
    client, manager = client_and_manager
    shell = "<html><bip-table></bip-table></html>"
    # The marker never appears → wait times out but still returns the DOM.
    page = FakePage(contents=[shell], rendered=shell)
    resp = await _post_rendered(
        client, manager, page,
        {"wait_for_text": "downloadDocument", "timeout_ms": 300},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["wait_satisfied"] is False
    assert data["html"] == shell


@pytest.mark.asyncio
async def test_wait_for_selector_satisfied(client_and_manager):
    client, manager = client_and_manager
    rendered = "<html><table id='documents'>rows</table></html>"
    page = FakePage(contents=[rendered], rendered=rendered, selector_ok=True)
    resp = await _post_rendered(
        client, manager, page,
        {"wait_for_selector": "table#documents", "timeout_ms": 5000},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["wait_satisfied"] is True
    assert page.selector_waits == 1
    assert data["html"] == rendered


@pytest.mark.asyncio
async def test_wait_for_selector_timeout_returns_unsatisfied(client_and_manager):
    client, manager = client_and_manager
    shell = "<html><bip-table></bip-table></html>"
    page = FakePage(contents=[shell], rendered=shell, selector_ok=False)
    resp = await _post_rendered(
        client, manager, page,
        {"wait_for_selector": "table#documents", "timeout_ms": 200},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["wait_satisfied"] is False
    assert data["html"] == shell


@pytest.mark.asyncio
async def test_rendered_html_requires_token(client_and_manager):
    client, manager = client_and_manager
    page = FakePage(contents=["<html></html>"], rendered="<html></html>")
    manager._sessions["delta"] = SimpleNamespace(page=page)
    try:
        resp = await client.post(
            "/session/delta/rendered-html",
            json={"wait_for_text": "x"},
            headers={"X-Bridge-Token": "wrong"},
        )
        assert resp.status_code == 401
    finally:
        manager._sessions.pop("delta", None)
        await client.aclose()
