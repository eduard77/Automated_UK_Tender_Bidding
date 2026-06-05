"""Bridge client surface — HTTP and in-process implementations.

Historically the bridge ran as its own service over HTTP (Windows host +
Linux container). Delta cloud stage 2 collapses both sides into the same
process when `settings.bridge_url` is empty — see `bridge_in_process.py`.
The HTTP client below still works for local-dev users who already run the
standalone bridge; `make_bridge_client()` picks the right implementation.

The dataclasses (BridgeError / BridgeFile / BridgeRowDownload / RenderedPage)
are shared between both implementations, so the orchestrator and the Delta
adapter import them from here exactly as before.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
import structlog

from tender_agent.config import settings

logger = structlog.get_logger(__name__)


class BridgeError(RuntimeError):
    """Raised when a bridge call fails (HTTP or in-process)."""


@dataclass
class BridgeFile:
    path: str  # filename inside the bridge download dir
    size_bytes: int
    mime_type: str | None = None


@dataclass
class BridgeRowDownload:
    """Result of click-download-in-row: the captured file (relative to the
    bridge download dir) plus the browser's suggested filename, which the
    caller uses to name the document and derive its extension."""

    path: str
    suggested_filename: str | None = None
    size_bytes: int = 0
    mime_type: str | None = None


@dataclass
class RenderedPage:
    """Rendered DOM plus whether the wait condition was satisfied before the
    read. wait_satisfied=False means the wait timed out — the html is still
    whatever rendered."""

    html: str
    wait_satisfied: bool
    current_url: str | None = None


@dataclass
class MarkerAlternative:
    """One alternative of an OR'd login marker and where the probe found it.

    `found` = the selector matched something in some frame's DOM; `visible` =
    at least one match is actually visible; `frame` names where it was found
    ('main' for the top document, otherwise a child frame's name or URL).
    `error` carries a locator/timeout error repr when one occurred — surfaced
    rather than swallowed so a false read can be explained (no cookie values).
    """

    label: str
    selector: str
    found: bool = False
    visible: bool = False
    frame: str | None = None
    error: str | None = None


@dataclass
class MarkerProbeResult:
    """Result of a frame-aware login-marker probe.

    `visible` is the headline boolean — any alternative visible in any frame.
    The per-alternative + per-frame detail (`alternatives`, `frames_searched`)
    plus `page_title` let `/session/test` report WHY a probe read false. This
    is the in-process port of the capture helper's detection (PR #92) so the
    headless cloud probe and the operator's capture agree on "logged in".
    """

    visible: bool
    page_title: str | None = None
    current_url: str | None = None
    frames_searched: list[str] = field(default_factory=list)
    alternatives: list[MarkerAlternative] = field(default_factory=list)
    error: str | None = None


class BridgeClient(Protocol):
    """The surface every caller uses (orchestrator, Delta adapter, Proactis
    discovery, preflight, the system API). Both `HttpBridgeClient` and
    `bridge_in_process.InProcessBridgeClient` satisfy this Protocol.

    Annotate parameters and return types with `BridgeClient` and let
    `make_bridge_client()` pick the implementation at runtime.
    """

    async def bridge_available(self) -> bool: ...
    async def open_session(
        self, platform_slug: str, start_url: str | None
    ) -> dict: ...
    async def session_status(self, slug: str) -> dict: ...
    async def wait_for_login(
        self,
        slug: str,
        success_url_pattern: str,
        login_url: str | None = None,
        timeout_seconds: int = 600,
    ) -> dict: ...
    async def navigate(self, slug: str, url: str) -> dict: ...
    async def fill(self, slug: str, selector: str, value: str) -> dict: ...
    async def click(self, slug: str, selector: str) -> dict: ...
    async def element_exists(self, slug: str, selector: str) -> bool: ...
    async def select_option(
        self,
        slug: str,
        selector: str,
        value: str | None = None,
        label: str | None = None,
        index: int | None = None,
    ) -> dict: ...
    async def page_text(self, slug: str) -> str: ...
    async def page_html(self, slug: str) -> str: ...
    async def rendered_html(
        self,
        slug: str,
        *,
        wait_for_selector: str | None = None,
        wait_for_text: str | None = None,
        timeout_ms: int = 15000,
    ) -> RenderedPage: ...
    async def find_links(self, slug: str, pattern: str) -> list[str]: ...
    async def download(
        self, slug: str, url: str, dest_filename: str | None = None
    ) -> BridgeFile: ...
    async def click_download(
        self, slug: str, selector: str, dest_filename: str | None = None
    ) -> BridgeFile: ...
    async def click_download_in_row(
        self,
        slug: str,
        *,
        table_selector: str,
        row_index: int,
        menu_trigger_selector: str,
        download_item_text: str,
        timeout_ms: int = 30000,
    ) -> BridgeRowDownload: ...
    async def screenshot(self, slug: str, label: str = "screenshot") -> dict: ...
    async def close_session(self, slug: str) -> dict: ...


def make_bridge_client(
    base_url: str | None = None,
    token: str | None = None,
    timeout: float | None = None,
) -> BridgeClient:
    """Factory — returns the HTTP client when `TENDER_AGENT_BRIDGE_URL` is
    set (back-compat for local-dev users with the standalone Windows bridge
    still running), otherwise the in-process Playwright driver.

    Caller-passed `base_url` overrides settings.bridge_url, useful for tests.
    """
    chosen_url = base_url if base_url is not None else settings.bridge_url
    if chosen_url:
        return HttpBridgeClient(
            base_url=base_url, token=token, timeout=timeout
        )
    # Lazy import — keeps `services.bridge_client` import-clean for unit
    # tests that monkeypatch httpx and never touch Playwright.
    from tender_agent.services.bridge_in_process import (  # noqa: PLC0415
        InProcessBridgeClient,
    )

    return InProcessBridgeClient(
        base_url=base_url, token=token, timeout=timeout
    )


class HttpBridgeClient:
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

    async def _post(
        self, path: str, json: dict | None = None, timeout: float | None = None
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=timeout or self.timeout) as client:
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

    async def select_option(
        self,
        slug: str,
        selector: str,
        value: str | None = None,
        label: str | None = None,
        index: int | None = None,
    ) -> dict:
        """Choose an option in a <select>. index=-1 picks the last option (used
        to maximise a page-size dropdown)."""
        body: dict[str, Any] = {"selector": selector}
        if value is not None:
            body["value"] = value
        if label is not None:
            body["label"] = label
        if index is not None:
            body["index"] = index
        return await self._post(f"/session/{slug}/select-option", body)

    async def page_text(self, slug: str) -> str:
        return (await self._get(f"/session/{slug}/page-text")).get("text", "")

    async def page_html(self, slug: str) -> str:
        return (await self._get(f"/session/{slug}/page-html")).get("html", "")

    async def rendered_html(
        self,
        slug: str,
        *,
        wait_for_selector: str | None = None,
        wait_for_text: str | None = None,
        timeout_ms: int = 15000,
    ) -> RenderedPage:
        """Return the RENDERED DOM after the page's JS has populated it.

        Unlike page_html (the initial server HTML), this waits for a selector OR
        for the content to contain a marker text before reading, so client-side
        rendered tables (Delta's bip-table) are present. On a wait timeout it
        still returns whatever rendered, with wait_satisfied=False."""
        body: dict[str, Any] = {"timeout_ms": timeout_ms}
        if wait_for_selector is not None:
            body["wait_for_selector"] = wait_for_selector
        if wait_for_text is not None:
            body["wait_for_text"] = wait_for_text
        # Give the HTTP call headroom over the bridge-side wait so the client
        # doesn't time out before the bridge returns its (possibly-unsatisfied)
        # result.
        http_timeout = max(self.timeout, timeout_ms / 1000.0 + 10.0)
        data = await self._post(
            f"/session/{slug}/rendered-html", body, timeout=http_timeout
        )
        return RenderedPage(
            html=data.get("html", ""),
            wait_satisfied=bool(data.get("wait_satisfied", False)),
            current_url=data.get("current_url"),
        )

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

    async def click_download_in_row(
        self,
        slug: str,
        *,
        table_selector: str,
        row_index: int,
        menu_trigger_selector: str,
        download_item_text: str,
        timeout_ms: int = 30000,
    ) -> BridgeRowDownload:
        """Open a table row's action menu (⋮) and click an item that triggers a
        file download, returning the captured file. For portals (Delta) whose
        rows carry no scrapable download link — the file only downloads on the
        per-row menu click. Raises BridgeError if no download is captured (so the
        caller can log that row and continue with the rest)."""
        # Give the HTTP call headroom over the bridge-side download wait so the
        # client doesn't time out before the bridge returns its result/error.
        http_timeout = max(self.timeout, timeout_ms / 1000.0 + 10.0)
        data = await self._post(
            f"/session/{slug}/click-download-in-row",
            {
                "table_selector": table_selector,
                "row_index": row_index,
                "menu_trigger_selector": menu_trigger_selector,
                "download_item_text": download_item_text,
                "timeout_ms": timeout_ms,
            },
            timeout=http_timeout,
        )
        return BridgeRowDownload(
            path=data["path"],
            suggested_filename=data.get("suggested_filename"),
            size_bytes=data.get("size_bytes", 0),
            mime_type=data.get("mime_type"),
        )

    async def screenshot(self, slug: str, label: str = "screenshot") -> dict:
        return await self._post(f"/session/{slug}/screenshot", {"label": label})

    async def close_session(self, slug: str) -> dict:
        return await self._post(f"/session/{slug}/close")
