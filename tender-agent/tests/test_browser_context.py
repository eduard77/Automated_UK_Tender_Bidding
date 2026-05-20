"""BrowserContextManager — context-path structure + launch wiring with a
fully mocked Playwright. No real browser is ever started.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tender_agent.services import browser as browser_mod
from tender_agent.services.browser import BrowserContextManager, context_dir


def test_context_dir_structure(tmp_path) -> None:
    path = context_dir("eduard", 42, root=str(tmp_path))
    assert path.endswith("eduard/42") or path.endswith("eduard\\42")


def test_context_path_uses_root(tmp_path) -> None:
    mgr = BrowserContextManager(root=str(tmp_path))
    p = mgr.context_path("alice", 7)
    assert str(tmp_path) in p
    assert p.endswith("7") or p.endswith("7/") or "alice" in p


@pytest.mark.asyncio
async def test_launch_respects_headless_and_creates_dir(tmp_path, monkeypatch) -> None:
    captured: dict = {}

    class FakePage:
        async def add_init_script(self, *_a, **_k):
            return None

    class FakeContext:
        def __init__(self) -> None:
            self.pages = [FakePage()]

        async def new_page(self):
            return FakePage()

        async def close(self):
            captured["context_closed"] = True

    class FakeChromium:
        async def launch_persistent_context(self, user_data_dir, **kwargs):
            captured["user_data_dir"] = user_data_dir
            captured["headless"] = kwargs.get("headless")
            captured["args"] = kwargs.get("args")
            return FakeContext()

    class FakePlaywright:
        def __init__(self) -> None:
            self.chromium = FakeChromium()

        async def stop(self):
            captured["pw_stopped"] = True

    class FakeAsyncPlaywrightCM:
        async def start(self):
            return FakePlaywright()

    # Patch the lazily-imported playwright entrypoint.
    import sys

    fake_module = SimpleNamespace(async_playwright=lambda: FakeAsyncPlaywrightCM())
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_module)
    # Make stealth import fail so we exercise the manual fallback path.
    monkeypatch.setitem(sys.modules, "playwright_stealth", None)

    mgr = BrowserContextManager(headless=True, root=str(tmp_path))
    launched = await mgr.launch("eduard", 99)

    assert captured["headless"] is True
    assert "eduard" in captured["user_data_dir"]
    assert "99" in captured["user_data_dir"]
    assert "--disable-blink-features=AutomationControlled" in captured["args"]

    await launched.close()
    assert captured.get("context_closed") is True
    assert captured.get("pw_stopped") is True


def test_default_root_env(monkeypatch) -> None:
    monkeypatch.setenv("TENDER_AGENT_BROWSER_ROOT", "/tmp/custom-root")
    assert browser_mod.default_browser_root() == "/tmp/custom-root"
