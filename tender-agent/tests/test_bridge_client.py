"""BridgeClient — every method hits the right endpoint with the token, and
bridge_available degrades gracefully when the bridge is down. httpx is faked.
"""
from __future__ import annotations

import json

import pytest

from tender_agent.services import bridge_client as bc_mod
from tender_agent.services.bridge_client import BridgeClient


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Records requests; returns a queued/canned response. Async context
    manager, mirroring httpx.AsyncClient's surface used by BridgeClient."""

    calls: list[dict] = []
    next_response: _FakeResponse = _FakeResponse(200, {})
    raise_on_get: Exception | None = None

    def __init__(self, *args, **kwargs):
        self.timeout = kwargs.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).calls.append({"method": "POST", "url": url, "json": json, "headers": headers})
        return type(self).next_response

    async def get(self, url, headers=None):
        if type(self).raise_on_get is not None:
            raise type(self).raise_on_get
        type(self).calls.append({"method": "GET", "url": url, "headers": headers})
        return type(self).next_response


@pytest.fixture(autouse=True)
def patch_httpx(monkeypatch):
    _FakeClient.calls = []
    _FakeClient.next_response = _FakeResponse(200, {})
    _FakeClient.raise_on_get = None
    monkeypatch.setattr(bc_mod.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(bc_mod.settings, "bridge_url", "http://bridge.test:8765")
    monkeypatch.setattr(bc_mod.settings, "bridge_token", "secret-token")
    yield


def _last():
    return _FakeClient.calls[-1]


@pytest.mark.asyncio
async def test_open_session_endpoint_and_token():
    _FakeClient.next_response = _FakeResponse(
        200, {"session_id": "delta", "current_url": "x", "authenticated_guess": False}
    )
    out = await BridgeClient().open_session("delta", "https://x")
    call = _last()
    assert call["url"] == "http://bridge.test:8765/session/open"
    assert call["headers"]["X-Bridge-Token"] == "secret-token"
    assert call["json"] == {"platform_slug": "delta", "start_url": "https://x"}
    assert out["session_id"] == "delta"


@pytest.mark.asyncio
async def test_wait_for_login_endpoint():
    _FakeClient.next_response = _FakeResponse(
        200, {"status": "logged_in", "current_url": "x"}
    )
    out = await BridgeClient().wait_for_login(
        "delta", r"/secure", login_url="https://l", timeout_seconds=30
    )
    call = _last()
    assert call["url"].endswith("/session/delta/wait-for-login")
    assert call["json"]["success_url_pattern"] == r"/secure"
    assert call["json"]["timeout_seconds"] == 30
    assert out["status"] == "logged_in"


@pytest.mark.asyncio
async def test_navigate_and_page_text_and_find_links():
    _FakeClient.next_response = _FakeResponse(
        200, {"current_url": "u", "status_code": 200, "title": "T"}
    )
    await BridgeClient().navigate("delta", "https://u")
    assert _last()["url"].endswith("/session/delta/navigate")

    _FakeClient.next_response = _FakeResponse(200, {"text": "hello", "current_url": "u"})
    text = await BridgeClient().page_text("delta")
    assert text == "hello"
    assert _last()["url"].endswith("/session/delta/page-text")

    _FakeClient.next_response = _FakeResponse(200, {"links": ["https://a/x.pdf"]})
    links = await BridgeClient().find_links("delta", r"\.pdf")
    assert links == ["https://a/x.pdf"]
    assert _last()["url"].endswith("/session/delta/find-links")


@pytest.mark.asyncio
async def test_rendered_html_endpoint_and_payload():
    _FakeClient.next_response = _FakeResponse(
        200,
        {"current_url": "u", "html": "<html>rows</html>", "wait_satisfied": True},
    )
    out = await BridgeClient().rendered_html(
        "delta", wait_for_text="downloadDocument", timeout_ms=15000
    )
    call = _last()
    assert call["url"].endswith("/session/delta/rendered-html")
    assert call["json"] == {"timeout_ms": 15000, "wait_for_text": "downloadDocument"}
    assert call["headers"]["X-Bridge-Token"] == "secret-token"
    assert out.html == "<html>rows</html>"
    assert out.wait_satisfied is True
    assert out.current_url == "u"


@pytest.mark.asyncio
async def test_rendered_html_selector_and_unsatisfied_wait():
    # Selector-based wait, and a timed-out wait degrades to wait_satisfied=False
    # (missing key also degrades to False) while still returning the html.
    _FakeClient.next_response = _FakeResponse(
        200, {"html": "<html>shell</html>"}
    )
    out = await BridgeClient().rendered_html(
        "delta", wait_for_selector="table#documents", timeout_ms=8000
    )
    assert _last()["json"] == {
        "timeout_ms": 8000, "wait_for_selector": "table#documents"
    }
    assert out.html == "<html>shell</html>"
    assert out.wait_satisfied is False


@pytest.mark.asyncio
async def test_fill_endpoint_and_payload():
    _FakeClient.next_response = _FakeResponse(200, {"ok": True, "current_url": "u"})
    await BridgeClient().fill("delta", "input#accessCode", "286EVX23TV")
    call = _last()
    assert call["url"].endswith("/session/delta/fill")
    assert call["json"] == {"selector": "input#accessCode", "value": "286EVX23TV"}
    assert call["headers"]["X-Bridge-Token"] == "secret-token"


@pytest.mark.asyncio
async def test_click_endpoint_and_payload():
    _FakeClient.next_response = _FakeResponse(200, {"ok": True, "current_url": "u"})
    await BridgeClient().click("delta", "button[type='submit']")
    call = _last()
    assert call["url"].endswith("/session/delta/click")
    assert call["json"] == {"selector": "button[type='submit']"}


@pytest.mark.asyncio
async def test_element_exists_returns_bool():
    _FakeClient.next_response = _FakeResponse(200, {"exists": True})
    assert await BridgeClient().element_exists("delta", "table#responses") is True
    assert _last()["url"].endswith("/session/delta/element-exists")

    _FakeClient.next_response = _FakeResponse(200, {"exists": False})
    assert await BridgeClient().element_exists("delta", "table#responses") is False

    # Missing key degrades to False rather than raising.
    _FakeClient.next_response = _FakeResponse(200, {})
    assert await BridgeClient().element_exists("delta", "x") is False


@pytest.mark.asyncio
async def test_select_option_endpoint_and_payload():
    _FakeClient.next_response = _FakeResponse(200, {"ok": True, "current_url": "u"})
    # index=-1 picks the last option (used to maximise a page-size dropdown).
    await BridgeClient().select_option("delta", "select.page-size", index=-1)
    call = _last()
    assert call["url"].endswith("/session/delta/select-option")
    assert call["json"] == {"selector": "select.page-size", "index": -1}

    # value/label are only sent when provided (omitted keys stay out of payload).
    _FakeClient.next_response = _FakeResponse(200, {"ok": True})
    await BridgeClient().select_option("delta", "select.page-size", value="100")
    assert _last()["json"] == {"selector": "select.page-size", "value": "100"}


@pytest.mark.asyncio
async def test_download_returns_bridge_file():
    _FakeClient.next_response = _FakeResponse(
        200, {"path": "abc.pdf", "size_bytes": 123, "mime_type": "application/pdf"}
    )
    bf = await BridgeClient().download("delta", "https://a/x.pdf", "abc.pdf")
    assert bf.path == "abc.pdf"
    assert bf.size_bytes == 123
    assert bf.mime_type == "application/pdf"


@pytest.mark.asyncio
async def test_error_status_raises():
    from tender_agent.services.bridge_client import BridgeError

    _FakeClient.next_response = _FakeResponse(500, {"detail": "boom"})
    with pytest.raises(BridgeError):
        await BridgeClient().navigate("delta", "https://u")


@pytest.mark.asyncio
async def test_bridge_available_true():
    _FakeClient.next_response = _FakeResponse(200, {"status": "ok"})
    assert await BridgeClient().bridge_available() is True


@pytest.mark.asyncio
async def test_bridge_available_false_when_down():
    _FakeClient.raise_on_get = ConnectionError("refused")
    assert await BridgeClient().bridge_available() is False


@pytest.mark.asyncio
async def test_close_session():
    _FakeClient.next_response = _FakeResponse(200, {"closed": True})
    out = await BridgeClient().close_session("delta")
    assert out == {"closed": True}
    assert _last()["url"].endswith("/session/delta/close")
