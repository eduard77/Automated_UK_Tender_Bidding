"""BrowserContextManager — persistent Playwright contexts per portal per user.

Storage layout (matches the design doc):
    {root}/{user_id}/{portal_id}/

Cookies + local storage persist across runs so a logged-in session survives.
Headless by default; pass headless=False for debugging. playwright-stealth is
applied best-effort — if the package is missing or its API has drifted, we log
and continue without it rather than failing context creation.

Playwright itself is imported lazily inside `launch` so the rest of the backend
(and the test suite, which mocks this class) never needs the browser binaries
installed.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


def default_browser_root() -> str:
    """Per-user persistent-context root. Honours TENDER_AGENT_BROWSER_ROOT,
    else ~/.tender-agent/browsers."""
    env = os.environ.get("TENDER_AGENT_BROWSER_ROOT")
    if env:
        return env
    return str(Path.home() / ".tender-agent" / "browsers")


def context_dir(user_id: str, portal_id: int, root: str | None = None) -> str:
    base = root or default_browser_root()
    return str(Path(base) / user_id / str(portal_id))


@dataclass
class LaunchedContext:
    """Handle returned by `launch`; close() tears everything down."""

    playwright: Any
    context: Any
    page: Any
    user_data_dir: str

    async def close(self) -> None:
        try:
            await self.context.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("browser.context_close_failed", error=str(exc))
        try:
            await self.playwright.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("browser.playwright_stop_failed", error=str(exc))


class BrowserContextManager:
    """Creates persistent, per-(user, portal) Playwright contexts."""

    def __init__(
        self,
        *,
        headless: bool = True,
        root: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        self.headless = headless
        self.root = root or default_browser_root()
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )

    def context_path(self, user_id: str, portal_id: int) -> str:
        return context_dir(user_id, portal_id, self.root)

    async def launch(self, user_id: str, portal_id: int) -> LaunchedContext:
        """Launch a persistent context for (user_id, portal_id).

        Lazily imports Playwright so importing this module never requires it.
        """
        user_data_dir = self.context_path(user_id, portal_id)
        os.makedirs(user_data_dir, exist_ok=True)

        from playwright.async_api import async_playwright  # noqa: PLC0415

        pw = await async_playwright().start()
        context = await pw.chromium.launch_persistent_context(
            user_data_dir,
            headless=self.headless,
            user_agent=self.user_agent,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await self._apply_stealth(page)
        return LaunchedContext(
            playwright=pw,
            context=context,
            page=page,
            user_data_dir=user_data_dir,
        )

    async def _apply_stealth(self, page: Any) -> None:
        """Best-effort anti-bot hardening. Never fails context creation."""
        try:
            from playwright_stealth import stealth_async  # noqa: PLC0415

            await stealth_async(page)
            return
        except Exception as exc:  # noqa: BLE001
            logger.info("browser.stealth_unavailable", error=str(exc))
        # Minimal manual fallback: hide the webdriver flag.
        try:
            await page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', "
                "{get: () => undefined});"
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("browser.manual_stealth_failed", error=str(exc))


__all__ = [
    "BrowserContextManager",
    "LaunchedContext",
    "context_dir",
    "default_browser_root",
]
