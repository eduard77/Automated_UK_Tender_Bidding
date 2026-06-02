"""In-process Playwright driver (Delta cloud, stage 2).

Same surface as `services.bridge_client.HttpBridgeClient` — every public method
matches signature, return shape, and BridgeError-on-failure semantics — but the
implementation drives Playwright in this process instead of over HTTP to a
separate bridge service.

Why this exists
---------------
The browser-bridge was only ever split out as its own service because the
backend ran in Docker (Linux) and the operator's browser had to be a headed
Windows window. In Azure both are Linux containers; the HTTP split is pure
overhead — and an extra moving part for the orchestrator's `needs_user_confirmation`
keepalive to babysit. This module collapses the two without touching the
Delta adapter, the orchestrator state machine, or the human-pause gate.

Lifecycle
---------
The Playwright instance + browser contexts are PROCESS-GLOBAL state (see
`_GlobalManager` below). Each `InProcessBridgeClient` is cheap to construct
and just forwards to that shared manager — re-entry from many orchestrator
tasks is safe. Shutdown is plumbed via `services.bridge_runtime.shutdown()`
which the FastAPI lifespan calls.

Stage 3 seam (NOT built here)
-----------------------------
Login model B is operator-exported `storage_state.json`. Today the persistent
context is initialised against `bridge_state_dir/<slug>/`. Stage 3 will add an
endpoint that drops a `storage_state.json` into that directory before
`/session/open` is called for the slug; this driver already loads any state it
finds because Playwright's `launch_persistent_context` reads the user_data_dir
contents on start.

Single-Delta-session lease (stage 5)
------------------------------------
Delta enforces ONE active login per supplier account. The lease will live in
the orchestrator (a DB-row lock keyed by `(platform_slug, supplier_account)`
checked before `open_session`). This driver is the natural place to acquire
and release it; the hook will land in `_GlobalManager.open` once stage 5
ships. NO auto-registration logic — that's a hard product invariant.
"""
from __future__ import annotations

import asyncio
import contextlib
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Any

import structlog

from tender_agent.config import settings
from tender_agent.services.bridge_client import (
    BridgeError,
    BridgeFile,
    BridgeRowDownload,
    RenderedPage,
)

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Process-global state
# ---------------------------------------------------------------------------


class _Session:
    """One persistent Playwright context + page per platform_slug.

    Mirrors `browser-bridge/bridge/sessions.BridgeSession` but lives in the
    backend process. No HTTP boundary, no shared mounts.
    """

    __slots__ = ("slug", "playwright", "context", "page")

    def __init__(self, slug: str, playwright: Any, context: Any, page: Any) -> None:
        self.slug = slug
        self.playwright = playwright
        self.context = context
        self.page = page


class _GlobalManager:
    """Singleton owning Playwright + a per-slug session cache. Re-entrant from
    multiple orchestrator tasks under an asyncio.Lock; one browser context per
    slug, reused until close()."""

    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}
        self._lock = asyncio.Lock()

    async def open(self, slug: str, start_url: str | None) -> _Session:
        async with self._lock:
            session = self._sessions.get(slug)
            if session is None:
                session = await self._launch(slug)
                self._sessions[slug] = session
        if start_url:
            with contextlib.suppress(Exception):
                await session.page.goto(start_url, wait_until="domcontentloaded")
        return session

    async def _launch(self, slug: str) -> _Session:
        try:
            from playwright.async_api import async_playwright  # noqa: PLC0415
        except ModuleNotFoundError as exc:
            raise BridgeError(
                "Playwright is not installed in this backend image — rebuild "
                "with the Playwright-bearing Dockerfile (Delta cloud stage 2)."
            ) from exc

        pw = await async_playwright().start()
        user_data_dir = _state_dir_for_slug(slug)
        # Per-session download dir so concurrent slugs don't fight over file
        # names — `_ingest_bridge_file` reads from this same path.
        downloads_root = _download_dir()
        downloads_root.mkdir(parents=True, exist_ok=True)
        headless = _headless()
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if headless:
            # Required to run Chromium as root inside the Playwright-Python
            # image and to avoid the small default /dev/shm killing the
            # renderer.
            launch_args.extend(["--no-sandbox", "--disable-dev-shm-usage"])
        context = await pw.chromium.launch_persistent_context(
            str(user_data_dir),
            headless=headless,
            accept_downloads=True,
            downloads_path=str(downloads_root),
            args=launch_args,
        )
        page = context.pages[0] if context.pages else await context.new_page()
        logger.info("bridge.in_process.session_opened", slug=slug, headless=headless)
        return _Session(slug=slug, playwright=pw, context=context, page=page)

    def get(self, slug: str) -> _Session | None:
        return self._sessions.get(slug)

    async def close(self, slug: str) -> bool:
        async with self._lock:
            session = self._sessions.pop(slug, None)
        if session is None:
            return False
        try:
            await session.context.close()
        finally:
            with contextlib.suppress(Exception):
                await session.playwright.stop()
        return True

    async def shutdown(self) -> None:
        for slug in list(self._sessions.keys()):
            with contextlib.suppress(Exception):
                await self.close(slug)


_manager: _GlobalManager | None = None


def _get_manager() -> _GlobalManager:
    global _manager
    if _manager is None:
        _manager = _GlobalManager()
    return _manager


async def shutdown_in_process_bridge() -> None:
    """Called from main.lifespan on shutdown so we close Chromium cleanly."""
    if _manager is not None:
        await _manager.shutdown()


# ---------------------------------------------------------------------------
# Config-derived helpers (env-overridable; mirror the standalone bridge)
# ---------------------------------------------------------------------------


def _state_dir_for_slug(slug: str) -> Path:
    """Per-slug Playwright user_data_dir. Cookies + localStorage live here;
    stage 3 will land `storage_state.json` here for Delta."""
    base = Path(getattr(settings, "bridge_state_dir", "/data/bridge-sessions"))
    base.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in (slug or "default") if c.isalnum() or c in "-_") or "default"
    d = base / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def _download_dir() -> Path:
    """Where Chromium writes captured downloads. Same path the Delta adapter's
    `_ingest_bridge_file` reads (no shared volume needed — same process)."""
    return Path(settings.bridge_download_dir)


def _headless() -> bool:
    """Headless by default in cloud; an operator can flip via env for a local
    headed run if they ever want to drive a first-time login in-process. The
    standalone bridge stays the recommended local-dev path for headed work."""
    raw = os.environ.get("TENDER_AGENT_BRIDGE_HEADLESS")
    if raw is None:
        return True
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Public driver — same surface as services.bridge_client.HttpBridgeClient
# ---------------------------------------------------------------------------


class InProcessBridgeClient:
    """Implements the BridgeClient Protocol against an in-process Playwright.

    Constructor takes the same `base_url`/`token`/`timeout` keyword arguments
    as the HTTP client (they're ignored here) so callers can swap
    implementations without changing instantiation sites.
    """

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float | None = None,
    ) -> None:
        # Kept on `self` only so callers that introspect — e.g. logging — see
        # the same fields HTTP exposed. Both are unused at runtime.
        self.base_url = "in-process"
        self.token = token if token is not None else ""
        self.timeout = timeout or 60.0
        self._manager = _get_manager()

    # --- availability --------------------------------------------------

    async def bridge_available(self) -> bool:
        """The in-process bridge is available iff Playwright + Chromium import
        cleanly. We don't actually launch a browser here — that would burn
        startup time on a probe — but we DO require the import path so the
        check fails fast if the image is missing the Playwright runtime."""
        try:
            import playwright  # noqa: F401, PLC0415

            return True
        except Exception as exc:  # noqa: BLE001
            logger.info("bridge.in_process.unavailable", error=str(exc))
            return False

    # --- session -------------------------------------------------------

    async def open_session(self, platform_slug: str, start_url: str | None) -> dict:
        session = await self._manager.open(platform_slug, start_url)
        current_url, guess = await _page_state(session)
        return {
            "session_id": platform_slug,
            "current_url": current_url,
            "authenticated_guess": guess,
        }

    async def session_status(self, slug: str) -> dict:
        session = self._manager.get(slug)
        if session is None:
            return {"exists": False, "current_url": None, "authenticated_guess": False}
        current_url, guess = await _page_state(session)
        return {"exists": True, "current_url": current_url, "authenticated_guess": guess}

    async def wait_for_login(
        self,
        slug: str,
        success_url_pattern: str,
        login_url: str | None = None,
        timeout_seconds: int = 600,
    ) -> dict:
        session = self._require(slug)
        page = session.page
        if login_url:
            with contextlib.suppress(Exception):
                await page.goto(login_url, wait_until="domcontentloaded")
        with contextlib.suppress(Exception):
            await page.bring_to_front()
        pattern = re.compile(success_url_pattern)
        deadline = time.monotonic() + max(2, timeout_seconds)
        while time.monotonic() < deadline:
            current_url = page.url or ""
            if pattern.search(current_url):
                return {"status": "logged_in", "current_url": current_url}
            await asyncio.sleep(2)
        return {"status": "timeout", "current_url": page.url}

    async def navigate(self, slug: str, url: str) -> dict:
        session = self._require(slug)
        try:
            resp = await session.page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            raise BridgeError(f"navigation failed: {exc}") from exc
        title = ""
        with contextlib.suppress(Exception):
            title = await session.page.title()
        return {
            "current_url": session.page.url,
            "status_code": resp.status if resp else None,
            "title": title,
        }

    async def fill(self, slug: str, selector: str, value: str) -> dict:
        session = self._require(slug)
        try:
            await session.page.fill(selector, value)
        except Exception as exc:  # noqa: BLE001
            raise BridgeError(f"fill failed: {exc}") from exc
        return {"ok": True, "current_url": session.page.url}

    async def click(self, slug: str, selector: str) -> dict:
        session = self._require(slug)
        try:
            await session.page.click(selector)
            with contextlib.suppress(Exception):
                await session.page.wait_for_load_state("domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            raise BridgeError(f"click failed: {exc}") from exc
        title = ""
        with contextlib.suppress(Exception):
            title = await session.page.title()
        return {"ok": True, "current_url": session.page.url, "title": title}

    async def element_exists(self, slug: str, selector: str) -> bool:
        session = self._require(slug)
        try:
            el = await session.page.query_selector(selector)
        except Exception:  # noqa: BLE001
            el = None
        return el is not None

    async def select_option(
        self,
        slug: str,
        selector: str,
        value: str | None = None,
        label: str | None = None,
        index: int | None = None,
    ) -> dict:
        session = self._require(slug)
        page = session.page
        try:
            if index is not None:
                idx = index
                if idx < 0:
                    count = await page.eval_on_selector(
                        selector, "el => (el.options ? el.options.length : 0)"
                    )
                    idx = max(0, (count or 0) + idx)
                await page.select_option(selector, index=idx)
            elif value is not None:
                await page.select_option(selector, value=value)
            elif label is not None:
                await page.select_option(selector, label=label)
            else:
                raise BridgeError("select-option needs value, label, or index")
            with contextlib.suppress(Exception):
                await page.wait_for_load_state("networkidle")
        except BridgeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BridgeError(f"select-option failed: {exc}") from exc
        return {"ok": True, "current_url": page.url}

    async def page_text(self, slug: str) -> str:
        session = self._require(slug)
        try:
            return await session.page.inner_text("body")
        except Exception:  # noqa: BLE001
            return ""

    async def page_html(self, slug: str) -> str:
        session = self._require(slug)
        try:
            return await session.page.content()
        except Exception:  # noqa: BLE001
            return ""

    async def rendered_html(
        self,
        slug: str,
        *,
        wait_for_selector: str | None = None,
        wait_for_text: str | None = None,
        timeout_ms: int = 15000,
    ) -> RenderedPage:
        session = self._require(slug)
        page = session.page
        timeout_ms = max(0, timeout_ms)
        wait_satisfied = True
        if wait_for_selector:
            wait_satisfied = False
            try:
                await page.wait_for_selector(wait_for_selector, timeout=timeout_ms)
                wait_satisfied = True
            except Exception:  # noqa: BLE001
                wait_satisfied = False
        elif wait_for_text:
            wait_satisfied = False
            deadline = time.monotonic() + timeout_ms / 1000.0
            while True:
                try:
                    content = await page.content()
                except Exception:  # noqa: BLE001
                    content = ""
                if wait_for_text in content:
                    wait_satisfied = True
                    break
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(0.25)
        try:
            html = await page.evaluate("() => document.documentElement.outerHTML")
        except Exception:  # noqa: BLE001
            try:
                html = await page.content()
            except Exception:  # noqa: BLE001
                html = ""
        return RenderedPage(
            html=html, wait_satisfied=wait_satisfied, current_url=page.url
        )

    async def find_links(self, slug: str, pattern: str) -> list[str]:
        session = self._require(slug)
        try:
            hrefs: list[str] = await session.page.eval_on_selector_all(
                "a[href]", "els => els.map(e => e.href)"
            )
        except Exception:  # noqa: BLE001
            return []
        regex = re.compile(pattern)
        return [h for h in hrefs if h and regex.search(h)]

    async def download(
        self, slug: str, url: str, dest_filename: str | None = None
    ) -> BridgeFile:
        session = self._require(slug)
        try:
            resp = await session.context.request.get(url)
        except Exception as exc:  # noqa: BLE001
            raise BridgeError(f"download failed: {exc}") from exc
        if resp.status >= 400:
            raise BridgeError(f"download http {resp.status}")
        data = await resp.body()
        filename = _safe_name(dest_filename or _name_from_url(url))
        path = _download_dir() / filename
        path.write_bytes(data)
        mime = (
            (resp.headers.get("content-type", "") or "").split(";")[0]
            or _guess_mime(filename)
        )
        return BridgeFile(path=filename, size_bytes=len(data), mime_type=mime)

    async def click_download(
        self, slug: str, selector: str, dest_filename: str | None = None
    ) -> BridgeFile:
        session = self._require(slug)
        page = session.page
        try:
            async with page.expect_download() as dl_info:
                await page.click(selector)
            dl = await dl_info.value
        except Exception as exc:  # noqa: BLE001
            raise BridgeError(f"click-download failed: {exc}") from exc
        filename = _safe_name(
            dest_filename or dl.suggested_filename or "download.bin"
        )
        path = _download_dir() / filename
        await dl.save_as(str(path))
        size = path.stat().st_size if path.exists() else 0
        return BridgeFile(
            path=filename, size_bytes=size, mime_type=_guess_mime(filename)
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
        """The Delta-specific row-action download. 1:1 port of the bridge
        server's logic — same selectors, same data-row index resolution, same
        page-wide get_by_text .last popup match, same per-row unique filename.
        Do not change without the Delta selector audit."""
        session = self._require(slug)
        page = session.page
        timeout_ms = max(1, timeout_ms)

        try:
            all_rows = await page.query_selector_all(f"{table_selector} tr")
        except Exception as exc:  # noqa: BLE001
            raise BridgeError(f"row lookup failed: {exc}") from exc
        data_rows = []
        for r in all_rows:
            with contextlib.suppress(Exception):
                if await r.query_selector("td") is not None:
                    data_rows.append(r)
        if not 0 <= row_index < len(data_rows):
            raise BridgeError(
                f"row index {row_index} out of range "
                f"({len(data_rows)} data rows)"
            )
        row = data_rows[row_index]

        trigger = None
        with contextlib.suppress(Exception):
            trigger = await row.query_selector(menu_trigger_selector)
        if trigger is None:
            raise BridgeError(
                f"action-menu trigger not found in row {row_index}"
            )
        try:
            await trigger.click()
        except Exception as exc:  # noqa: BLE001
            raise BridgeError(f"opening row menu failed: {exc}") from exc

        item = page.get_by_text(download_item_text, exact=False).last
        try:
            async with page.expect_download(timeout=timeout_ms) as dl_info:
                await item.click(timeout=timeout_ms)
            dl = await dl_info.value
        except Exception as exc:  # noqa: BLE001
            raise BridgeError(
                f"no download captured for row {row_index}: {exc}"
            ) from exc

        suggested = None
        with contextlib.suppress(Exception):
            suggested = dl.suggested_filename
        filename = _safe_name(f"r{row_index}_{suggested or 'download.bin'}")
        path = _download_dir() / filename
        await dl.save_as(str(path))
        size = path.stat().st_size if path.exists() else 0
        return BridgeRowDownload(
            path=filename,
            suggested_filename=suggested,
            size_bytes=size,
            mime_type=_guess_mime(suggested or filename),
        )

    async def screenshot(self, slug: str, label: str = "screenshot") -> dict:
        session = self._require(slug)
        filename = _safe_name(f"{label}.png")
        path = _download_dir() / filename
        await session.page.screenshot(path=str(path), full_page=True)
        return {
            "path": filename,
            "size_bytes": path.stat().st_size if path.exists() else 0,
        }

    async def close_session(self, slug: str) -> dict:
        closed = await self._manager.close(slug)
        return {"closed": closed}

    # --- helpers ------------------------------------------------------

    def _require(self, slug: str) -> _Session:
        session = self._manager.get(slug)
        if session is None:
            raise BridgeError(f"session not open: {slug}")
        return session


# ---------------------------------------------------------------------------
# Page-state + filename helpers (mirrors browser-bridge/bridge/server helpers)
# ---------------------------------------------------------------------------


async def _page_state(session: _Session) -> tuple[str, bool]:
    page = session.page
    current_url = page.url
    try:
        html = await page.content()
    except Exception:  # noqa: BLE001
        html = ""
    return current_url, _authenticated_guess(current_url, html)


def _authenticated_guess(current_url: str, html: str) -> bool:
    """True if the page is NOT obviously a login page. The adapter does the
    real per-portal auth detection; this is just a coarse hint the dashboard
    can use to render."""
    if "login" in (current_url or "").lower():
        return False
    markers = re.compile(
        r"(type=[\"']password[\"']|sign in|log in|login|username|forgot password)",
        re.IGNORECASE,
    )
    return not markers.search(html or "")


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_name(name: str) -> str:
    name = (name or "file").replace("\\", "/").split("/")[-1]
    name = _SAFE_NAME_RE.sub("_", name).lstrip("._")
    return name[:200] or "file"


def _name_from_url(url: str) -> str:
    tail = url.split("?")[0].rstrip("/").split("/")[-1]
    return tail or "download.bin"


def _guess_mime(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"
