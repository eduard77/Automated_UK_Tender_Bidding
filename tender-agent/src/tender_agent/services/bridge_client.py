"""Async client for the native Windows browser bridge.

The backend runs in Docker; the bridge runs on the Windows host. The container
reaches the host at host.docker.internal. Every call carries the shared token.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from tender_agent.config import settings

logger = structlog.get_logger(__name__)


class BridgeError(RuntimeError):
    """Raised when the bridge returns an error or is unreachable."""


@dataclass
class BridgeFile:
    path: str  # relative to the shared download dir
    size_bytes: int
    mime_type: str | None = None


class BridgeClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.base_url = (base_url or settings.bridge_url).rstrip("/")
        self.token = token if token is not None else settings.bridge_token
        self.timeout = timeout or 60.0

    def _headers(self) -> dict[str, str]:
        return {"X-Bridge-Token": self.token}

    async def _post(self, path: str, json: dict | None = None) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}{path}", json=json or {}, headers=self._headers()
            )
        if resp.status_code >= 400:
            raise BridgeError(f"bridge {path} -> HTTP {resp.status_code}: {resp.text}")
        return resp.json()

    async def _get(self, path: str, timeout: float | None = None) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=timeout or self.timeout) as client:
            resp = await client.get(f"{self.base_url}{path}", headers=self._headers())
        if resp.status_code >= 400:
            raise BridgeError(f"bridge {path} -> HTTP {resp.status_code}: {resp.text}")
        return resp.json()

    # --- availability --------------------------------------------------

    async def bridge_available(self) -> bool:
        """Fast health probe so the orchestrator can give a clear 'start the
        bridge' error instead of a confusing timeout."""
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{self.base_url}/health")
            return resp.status_code == 200
        except Exception as exc:  # noqa: BLE001
            logger.info("bridge.unavailable", error=str(exc))
            return False

    # --- session -------------------------------------------------------

    async def open_session(self, platform_slug: str, start_url: str | None) -> dict:
        return await self._post(
            "/session/open",
            {"platform_slug": platform_slug, "start_url": start_url},
        )

    async def session_status(self, slug: str) -> dict:
        return await self._get(f"/session/{slug}/status")

    async def wait_for_login(
        self,
        slug: str,
        success_url_pattern: str,
        login_url: str | None = None,
        timeout_seconds: int = 600,
    ) -> dict:
        return await self._post(
            f"/session/{slug}/wait-for-login",
            {
                "success_url_pattern": success_url_pattern,
                "login_url": login_url,
                "timeout_seconds": timeout_seconds,
            },
        )

    async def navigate(self, slug: str, url: str) -> dict:
        return await self._post(f"/session/{slug}/navigate", {"url": url})

    async def fill(self, slug: str, selector: str, value: str) -> dict:
        """Type a value into a form field (e.g. Delta's Access Code box)."""
        return await self._post(
            f"/session/{slug}/fill", {"selector": selector, "value": value}
        )

    async def click(self, slug: str, selector: str) -> dict:
        """Click a non-download control (e.g. a Submit button). Use
        click_download for controls that trigger a file download."""
        return await self._post(f"/session/{slug}/click", {"selector": selector})

    async def element_exists(self, slug: str, selector: str) -> bool:
        """Whether a selector matches anything on the current page."""
        data = await self._post(
            f"/session/{slug}/element-exists", {"selector": selector}
        )
        return bool(data.get("exists", False))

    async def page_text(self, slug: str) -> str:
        return (await self._get(f"/session/{slug}/page-text")).get("text", "")

    async def page_html(self, slug: str) -> str:
        return (await self._get(f"/session/{slug}/page-html")).get("html", "")

    async def find_links(self, slug: str, pattern: str) -> list[str]:
        return (
            await self._post(f"/session/{slug}/find-links", {"pattern": pattern})
        ).get("links", [])

    async def download(
        self, slug: str, url: str, dest_filename: str | None = None
    ) -> BridgeFile:
        data = await self._post(
            f"/session/{slug}/download",
            {"url": url, "dest_filename": dest_filename},
        )
        return BridgeFile(
            path=data["path"],
            size_bytes=data["size_bytes"],
            mime_type=data.get("mime_type"),
        )

    async def click_download(
        self, slug: str, selector: str, dest_filename: str | None = None
    ) -> BridgeFile:
        data = await self._post(
            f"/session/{slug}/click-download",
            {"selector": selector, "dest_filename": dest_filename},
        )
        return BridgeFile(
            path=data["path"],
            size_bytes=data["size_bytes"],
            mime_type=data.get("mime_type"),
        )

    async def screenshot(self, slug: str, label: str = "screenshot") -> dict:
        return await self._post(f"/session/{slug}/screenshot", {"label": label})

    async def close_session(self, slug: str) -> dict:
        return await self._post(f"/session/{slug}/close")
