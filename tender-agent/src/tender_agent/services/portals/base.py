"""PortalAdapter ABC + the per-call context object.

The six-method interface (matches_url, authenticate, locate_tender,
register_interest, download_documents, screenshot) mirrors the design doc
"Universal adapter framework". Adapters are built per *platform* (one Delta
adapter serves every Delta-hosted buyer), not per portal.

This prompt ships only the framework + the FallbackAdapter. Real platform
adapters arrive in later prompts.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import structlog

from tender_agent.services.portals.results import (
    AuthResult,
    DownloadResult,
    LocateResult,
    RegisterResult,
    ScreenshotResult,
)

logger = structlog.get_logger(__name__)


@dataclass
class Credentials:
    """Decrypted credentials handed to an adapter for a single run."""

    username: str | None = None
    password: str | None = None
    email: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PortalContext:
    """Everything an adapter needs for one orchestration run.

    `page` is a Playwright Page when the adapter `requires_browser`; for the
    FallbackAdapter (HTTP-only) it stays None and `http` carries an
    httpx.AsyncClient instead.
    """

    portal_id: int
    user_id: str
    domain: str
    page: Any | None = None
    http: Any | None = None
    # Candidate document/tender URLs the orchestrator already knows about
    # (from the tender's documents[] and link-type sightings). The
    # FallbackAdapter downloads straight from these.
    candidate_urls: list[str] = field(default_factory=list)
    # Tender scope for on-disk storage layout. 0 when not run per-tender.
    tender_id: int = 0
    # Login-via-human flow (chunk 4): the browser bridge client + the bridge
    # session slug. Set for adapters with requires_login=True (e.g. Delta).
    bridge: Any | None = None
    platform_slug: str | None = None
    # The tender's source reference / direct portal URL, used by login adapters
    # to locate the tender page.
    tender_ref: str | None = None
    source_url: str | None = None
    # The tender's free-text description. Some portals (e.g. Delta) embed the
    # access code inside the notice text, so adapters parse this too.
    description: str | None = None


class PortalAdapter(ABC):
    """Subclass per e-tendering platform."""

    # The platform slug this adapter serves (None for the catch-all fallback).
    platform_slug: str | None = None
    # When True the orchestrator provisions a Playwright browser context before
    # calling the adapter. FallbackAdapter sets this False so the HTTP-only
    # path runs without Playwright installed (CI-safe).
    requires_browser: bool = True
    # When True the adapter needs an authenticated browser session via the
    # Windows bridge: the orchestrator opens a session, waits for the human to
    # log in, then drives the adapter. Login adapters never store passwords.
    requires_login: bool = False

    def login_url(self) -> str | None:
        """The portal's login page URL (for the wait-for-login step). Login
        adapters override this; others return None."""
        return None

    def login_success_pattern(self) -> str:
        """Regex matched against the page URL to detect a completed login.
        Default never matches, so the orchestrator falls back to polling
        is_authenticated; login adapters override with a real pattern."""
        return r"(?!)"

    async def is_authenticated(self, ctx: PortalContext) -> bool:
        """Whether the bridge session is already logged in. Non-login adapters
        are trivially 'authenticated' (no login needed)."""
        return True

    @abstractmethod
    def matches_url(self, url: str) -> bool:
        """Cheap classifier: does this adapter handle the given URL?"""

    @abstractmethod
    async def authenticate(
        self, ctx: PortalContext, creds: Credentials | None
    ) -> AuthResult:
        """Log in with stored credentials."""

    @abstractmethod
    async def locate_tender(
        self, ctx: PortalContext, tender_ref: str
    ) -> LocateResult:
        """Navigate to a specific tender by its reference."""

    @abstractmethod
    async def register_interest(self, ctx: PortalContext) -> RegisterResult:
        """Click 'register interest' / equivalent on the located tender."""

    @abstractmethod
    async def download_documents(
        self, ctx: PortalContext, dest_dir: str
    ) -> DownloadResult:
        """Download every reachable ITT document into dest_dir."""

    async def screenshot(
        self, ctx: PortalContext, label: str
    ) -> ScreenshotResult:
        """Default audit screenshot. Adapters with a browser page override or
        rely on this; HTTP-only adapters get a no-op."""
        page = ctx.page
        if page is None:
            return ScreenshotResult(captured=False, detail="no browser page")
        try:
            import os

            path = os.path.join(
                dest_dir_for_screenshot(ctx), f"{label}.png"
            )
            await page.screenshot(path=path)
            return ScreenshotResult(path=path, captured=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("portal_adapter.screenshot_failed", error=str(exc))
            return ScreenshotResult(captured=False, detail=str(exc))


def dest_dir_for_screenshot(ctx: PortalContext) -> str:
    import os
    import tempfile

    base = os.path.join(tempfile.gettempdir(), "tender-agent-screenshots")
    os.makedirs(base, exist_ok=True)
    return base
