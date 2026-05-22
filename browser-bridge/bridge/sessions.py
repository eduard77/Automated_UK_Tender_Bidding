"""Persistent Playwright session management — one context per platform_slug.

A persistent context keeps cookies/localStorage on disk, so the user logs in
once and the session survives bridge restarts until it expires. Default HEADED
(a real visible window the user logs into); headless only for the automated
test.
"""
from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass
from typing import Any

from bridge import config


@dataclass
class BridgeSession:
    slug: str
    playwright: Any
    context: Any
    page: Any


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, BridgeSession] = {}
        self._lock = asyncio.Lock()

    async def open(self, slug: str, start_url: str | None) -> BridgeSession:
        async with self._lock:
            session = self._sessions.get(slug)
            if session is None:
                session = await self._launch(slug)
                self._sessions[slug] = session
        if start_url:
            # Navigation errors surface via /status; don't fail the open.
            with contextlib.suppress(Exception):
                await session.page.goto(start_url, wait_until="domcontentloaded")
        return session

    async def _launch(self, slug: str) -> BridgeSession:
        from playwright.async_api import async_playwright  # noqa: PLC0415

        pw = await async_playwright().start()
        user_data_dir = str(config.session_dir(slug))
        context = await pw.chromium.launch_persistent_context(
            user_data_dir,
            headless=config.headless(),
            accept_downloads=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        return BridgeSession(slug=slug, playwright=pw, context=context, page=page)

    def get(self, slug: str) -> BridgeSession | None:
        return self._sessions.get(slug)

    async def close(self, slug: str) -> bool:
        async with self._lock:
            session = self._sessions.pop(slug, None)
        if session is None:
            return False
        try:
            await session.context.close()
        finally:
            await session.playwright.stop()
        return True

    async def shutdown(self) -> None:
        for slug in list(self._sessions.keys()):
            await self.close(slug)


def looks_like_login(html: str) -> bool:
    """Cheap heuristic: does the page look like a login form?"""
    markers = re.compile(
        r"(type=[\"']password[\"']|sign in|log in|login|username|forgot password)",
        re.IGNORECASE,
    )
    return bool(markers.search(html or ""))


def authenticated_guess(current_url: str, html: str) -> bool:
    """True if the page is NOT obviously a login page. Real per-portal auth
    detection is done by the adapter; this is only a hint for the dashboard."""
    if "login" in (current_url or "").lower():
        return False
    return not looks_like_login(html)
