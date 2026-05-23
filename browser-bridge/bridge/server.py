"""The browser bridge FastAPI app.

Runs natively on Windows (NOT in Docker) and owns a real, visible Chrome via
Playwright. The Docker backend drives it over this local HTTP API, secured by a
shared token. The user logs in by hand in the visible window; the bridge never
sees or stores a password — only the resulting cookies (on disk, in the
persistent context).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import mimetypes
import re
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from bridge import config
from bridge.sessions import SessionManager, authenticated_guess

manager = SessionManager()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        yield
    finally:
        await manager.shutdown()


app = FastAPI(title="Tender Agent Browser Bridge", version="0.1.0", lifespan=lifespan)


def require_token(x_bridge_token: str | None = Header(default=None)) -> None:
    """Reject anything that doesn't present the shared token. This is what stops
    arbitrary local processes from driving the user's logged-in browser."""
    expected = config.bridge_token()
    if not expected or x_bridge_token != expected:
        raise HTTPException(status_code=401, detail="invalid or missing bridge token")


# --- request models -----------------------------------------------------


class OpenSessionBody(BaseModel):
    platform_slug: str
    start_url: str | None = None


class WaitForLoginBody(BaseModel):
    success_url_pattern: str
    login_url: str | None = None
    timeout_seconds: int = 600


class NavigateBody(BaseModel):
    url: str


class FindLinksBody(BaseModel):
    pattern: str


class DownloadBody(BaseModel):
    url: str
    dest_filename: str | None = None


class ClickDownloadBody(BaseModel):
    selector: str
    dest_filename: str | None = None
    timeout_ms: int = 30000


class ClickDownloadInRowBody(BaseModel):
    # Generic, portal-agnostic: the caller supplies the selectors. Used for
    # tables whose rows expose NO download link in the DOM (Delta's bip-table):
    # the file only downloads after the row's action menu is opened and the
    # "Download File" item is clicked.
    rows_selector: str  # all table rows, e.g. "table#document tbody tr"
    trigger_selector: str  # the action-menu (⋮) trigger, relative to a row
    item_selector: str  # the "Download File" item revealed by the trigger
    index: int  # which DATA row (0-based; header/empty rows are skipped)
    timeout_ms: int = 30000
    dest_filename: str | None = None


class FillBody(BaseModel):
    selector: str
    value: str


class ClickBody(BaseModel):
    selector: str


class SelectorBody(BaseModel):
    selector: str


class SelectOptionBody(BaseModel):
    selector: str
    value: str | None = None
    label: str | None = None
    # index = -1 selects the LAST option (used to pick the largest page size).
    index: int | None = None


class ScreenshotBody(BaseModel):
    label: str = "screenshot"


class RenderedHtmlBody(BaseModel):
    # Wait for one of these before reading the DOM (JS-rendered tables, e.g.
    # Delta's bip-table, are absent from the initial server HTML). If both are
    # given, the selector is waited for first. Either may be omitted.
    wait_for_selector: str | None = None
    wait_for_text: str | None = None
    timeout_ms: int = 15000


# --- health (no token: availability probe only) ------------------------


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "browser-bridge", "version": "0.1.0"}


# --- session endpoints (all require the token) -------------------------


async def _page_state(session) -> tuple[str, bool]:
    page = session.page
    current_url = page.url
    try:
        html = await page.content()
    except Exception:  # noqa: BLE001
        html = ""
    return current_url, authenticated_guess(current_url, html)


def _require_session(slug: str):
    session = manager.get(slug)
    if session is None:
        raise HTTPException(status_code=404, detail="session not open")
    return session


@app.post("/session/open")
async def open_session(
    body: OpenSessionBody, _: None = Depends(require_token)
) -> dict:
    session = await manager.open(body.platform_slug, body.start_url)
    current_url, guess = await _page_state(session)
    return {
        "session_id": body.platform_slug,
        "current_url": current_url,
        "authenticated_guess": guess,
    }


@app.get("/session/{slug}/status")
async def session_status(slug: str, _: None = Depends(require_token)) -> dict:
    session = manager.get(slug)
    if session is None:
        return {"exists": False, "current_url": None, "authenticated_guess": False}
    current_url, guess = await _page_state(session)
    return {"exists": True, "current_url": current_url, "authenticated_guess": guess}


@app.post("/session/{slug}/wait-for-login")
async def wait_for_login(
    slug: str, body: WaitForLoginBody, _: None = Depends(require_token)
) -> dict:
    session = _require_session(slug)
    page = session.page
    if body.login_url:
        with contextlib.suppress(Exception):
            await page.goto(body.login_url, wait_until="domcontentloaded")
    # Bring the visible window forward so the user notices it.
    with contextlib.suppress(Exception):
        await page.bring_to_front()

    pattern = re.compile(body.success_url_pattern)
    deadline = time.monotonic() + max(2, body.timeout_seconds)
    while time.monotonic() < deadline:
        current_url = page.url or ""
        if pattern.search(current_url):
            return {"status": "logged_in", "current_url": current_url}
        await asyncio.sleep(2)
    return {"status": "timeout", "current_url": page.url}


@app.post("/session/{slug}/navigate")
async def navigate(
    slug: str, body: NavigateBody, _: None = Depends(require_token)
) -> dict:
    session = _require_session(slug)
    try:
        resp = await session.page.goto(body.url, wait_until="domcontentloaded")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"navigation failed: {exc}") from exc
    title = ""
    with contextlib.suppress(Exception):
        title = await session.page.title()
    return {
        "current_url": session.page.url,
        "status_code": resp.status if resp else None,
        "title": title,
    }


@app.get("/session/{slug}/page-text")
async def page_text(slug: str, _: None = Depends(require_token)) -> dict:
    session = _require_session(slug)
    try:
        text = await session.page.inner_text("body")
    except Exception:  # noqa: BLE001
        text = ""
    return {"current_url": session.page.url, "text": text}


@app.get("/session/{slug}/page-html")
async def page_html(slug: str, _: None = Depends(require_token)) -> dict:
    session = _require_session(slug)
    try:
        html = await session.page.content()
    except Exception:  # noqa: BLE001
        html = ""
    return {"current_url": session.page.url, "html": html}


@app.post("/session/{slug}/rendered-html")
async def rendered_html(
    slug: str, body: RenderedHtmlBody, _: None = Depends(require_token)
) -> dict:
    """Return the RENDERED DOM, optionally after waiting for the page to finish
    rendering JS-injected content.

    page-html returns the initial server HTML; some portals (Delta eSourcing's
    bip-table web components) inject the real rows — and their download links —
    client-side after load, so they are missing from that initial HTML. This
    endpoint waits for a selector OR for the page content to contain a marker
    text (e.g. "downloadDocument"), then returns document.documentElement
    .outerHTML. On timeout it still returns whatever has rendered, with
    wait_satisfied=false so the caller can decide what to do."""
    session = _require_session(slug)
    page = session.page
    timeout_ms = max(0, body.timeout_ms)
    wait_satisfied = True

    if body.wait_for_selector:
        wait_satisfied = False
        try:
            await page.wait_for_selector(body.wait_for_selector, timeout=timeout_ms)
            wait_satisfied = True
        except Exception:  # noqa: BLE001
            wait_satisfied = False
    elif body.wait_for_text:
        # Poll the live content until the marker text appears or we time out.
        wait_satisfied = False
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            try:
                content = await page.content()
            except Exception:  # noqa: BLE001
                content = ""
            if body.wait_for_text in content:
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
    return {
        "current_url": page.url,
        "html": html,
        "wait_satisfied": wait_satisfied,
    }


@app.post("/session/{slug}/find-links")
async def find_links(
    slug: str, body: FindLinksBody, _: None = Depends(require_token)
) -> dict:
    session = _require_session(slug)
    hrefs: list[str] = await session.page.eval_on_selector_all(
        "a[href]", "els => els.map(e => e.href)"
    )
    pattern = re.compile(body.pattern)
    matched = [h for h in hrefs if h and pattern.search(h)]
    return {"links": matched}


@app.post("/session/{slug}/download")
async def download(
    slug: str, body: DownloadBody, _: None = Depends(require_token)
) -> dict:
    session = _require_session(slug)
    # Use the context's request API so the download carries the session cookies.
    try:
        resp = await session.context.request.get(body.url)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"download failed: {exc}") from exc
    if resp.status >= 400:
        raise HTTPException(status_code=502, detail=f"download http {resp.status}")
    data = await resp.body()
    filename = _safe_name(body.dest_filename or _name_from_url(body.url))
    path = config.download_dir() / filename
    path.write_bytes(data)
    mime = resp.headers.get("content-type", "").split(";")[0] or _guess_mime(filename)
    return {
        "path": filename,
        "size_bytes": len(data),
        "mime_type": mime,
        "b64": base64.b64encode(data).decode("ascii") if len(data) < 1_000_000 else None,
    }


@app.post("/session/{slug}/click-download")
async def click_download(
    slug: str, body: ClickDownloadBody, _: None = Depends(require_token)
) -> dict:
    session = _require_session(slug)
    page = session.page
    try:
        async with page.expect_download(timeout=body.timeout_ms) as dl_info:
            await page.click(body.selector, timeout=body.timeout_ms)
        dl = await dl_info.value
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail=f"click-download failed: {exc}"
        ) from exc
    return await _save_download(dl, body.dest_filename)


@app.post("/session/{slug}/click-download-in-row")
async def click_download_in_row(
    slug: str, body: ClickDownloadInRowBody, _: None = Depends(require_token)
) -> dict:
    """Capture a download triggered by a row's action menu.

    Some tables (Delta eSourcing's bip-table) expose no per-row download link;
    the file only downloads after opening the row's action menu (the ⋮) and
    clicking "Download File". This opens that menu in the index-th DATA row,
    clicks the (now-visible) item, and captures the resulting Playwright
    download. DATA rows are matches of rows_selector that contain
    trigger_selector — so the index is stable even if a header row sits inside
    <tbody>. All selectors come from the caller (no portal hardcoding)."""
    session = _require_session(slug)
    page = session.page
    try:
        rows = page.locator(body.rows_selector)
        total = await rows.count()
        data_rows = []
        for k in range(total):
            cand = rows.nth(k)
            if await cand.locator(body.trigger_selector).count() > 0:
                data_rows.append(cand)
        if not 0 <= body.index < len(data_rows):
            raise HTTPException(
                status_code=404,
                detail=(
                    f"row index {body.index} out of range "
                    f"({len(data_rows)} data rows)"
                ),
            )
        row = data_rows[body.index]
        # 1. Open the row's action menu (the ⋮ trigger).
        await row.locator(body.trigger_selector).first.click(timeout=body.timeout_ms)
        # 2. Click the now-visible "Download File" item and capture the download.
        async with page.expect_download(timeout=body.timeout_ms) as dl_info:
            await _click_first_visible(page, body.item_selector, body.timeout_ms)
        dl = await dl_info.value
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail=f"click-download-in-row failed: {exc}"
        ) from exc
    return await _save_download(dl, body.dest_filename)


@app.post("/session/{slug}/fill")
async def fill(slug: str, body: FillBody, _: None = Depends(require_token)) -> dict:
    """Type a value into a text input (e.g. Delta's Access Code box). The human
    still does the login; this only drives post-login form fields."""
    session = _require_session(slug)
    try:
        await session.page.fill(body.selector, body.value)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"fill failed: {exc}") from exc
    return {"ok": True, "current_url": session.page.url}


@app.post("/session/{slug}/click")
async def click(slug: str, body: ClickBody, _: None = Depends(require_token)) -> dict:
    """Click an element that does NOT trigger a download (e.g. a Submit button).
    For download-triggering clicks use /click-download instead."""
    session = _require_session(slug)
    try:
        await session.page.click(body.selector)
        with contextlib.suppress(Exception):
            await session.page.wait_for_load_state("domcontentloaded")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"click failed: {exc}") from exc
    title = ""
    with contextlib.suppress(Exception):
        title = await session.page.title()
    return {"ok": True, "current_url": session.page.url, "title": title}


@app.post("/session/{slug}/element-exists")
async def element_exists(
    slug: str, body: SelectorBody, _: None = Depends(require_token)
) -> dict:
    """Whether a selector matches anything on the current page. Lets adapters
    detect state (e.g. the Responses table, an Express Interest button) without
    parsing raw HTML."""
    session = _require_session(slug)
    try:
        el = await session.page.query_selector(body.selector)
    except Exception:  # noqa: BLE001
        el = None
    return {"exists": el is not None}


@app.post("/session/{slug}/select-option")
async def select_option(
    slug: str, body: SelectOptionBody, _: None = Depends(require_token)
) -> dict:
    """Choose an option in a <select> (e.g. Delta's "items per page" dropdown).
    index=-1 selects the last option, used to pick the largest page size so all
    rows render on one page."""
    session = _require_session(slug)
    page = session.page
    try:
        if body.index is not None:
            idx = body.index
            if idx < 0:
                count = await page.eval_on_selector(
                    body.selector, "el => (el.options ? el.options.length : 0)"
                )
                idx = max(0, (count or 0) + idx)
            await page.select_option(body.selector, index=idx)
        elif body.value is not None:
            await page.select_option(body.selector, value=body.value)
        elif body.label is not None:
            await page.select_option(body.selector, label=body.label)
        else:
            raise HTTPException(
                status_code=400, detail="select-option needs value, label, or index"
            )
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail=f"select-option failed: {exc}"
        ) from exc
    return {"ok": True, "current_url": page.url}


@app.post("/session/{slug}/screenshot")
async def screenshot(
    slug: str, body: ScreenshotBody, _: None = Depends(require_token)
) -> dict:
    session = _require_session(slug)
    filename = _safe_name(f"{body.label}.png")
    path = config.download_dir() / filename
    await session.page.screenshot(path=str(path), full_page=True)
    return {"path": filename, "size_bytes": path.stat().st_size if path.exists() else 0}


@app.post("/session/{slug}/close")
async def close_session(slug: str, _: None = Depends(require_token)) -> dict:
    """End a session: close its pages and the persistent browser context.

    This is the local half of releasing a session. For portals with a single
    concurrent-login constraint (Delta eSourcing), the adapter first navigates
    the live session to the portal's logout URL (via /navigate) so the *server*
    frees the login too — closing the context alone would leave Delta's session
    active until it times out, locking the real user out. The orchestrator does
    logout-then-close on every terminal state."""
    closed = await manager.close(slug)
    return {"closed": closed}


# --- helpers ------------------------------------------------------------


async def _save_download(dl, dest_filename: str | None) -> dict:
    """Save a captured Playwright download to the shared download dir and return
    its metadata (path is relative to that dir; the backend reads it there)."""
    filename = _safe_name(dest_filename or dl.suggested_filename or "download.bin")
    path = config.download_dir() / filename
    await dl.save_as(str(path))
    size = path.stat().st_size if path.exists() else 0
    return {
        "path": filename,
        "size_bytes": size,
        "mime_type": _guess_mime(filename),
        "suggested_filename": dl.suggested_filename,
    }


async def _click_first_visible(page, selector: str, timeout_ms: int) -> None:
    """Click the first VISIBLE match of selector. Delta renders the "Download
    File" item in a body-level popup and may keep hidden per-row duplicates in
    the DOM, so we must click the visible one rather than the first match."""
    loc = page.locator(selector)
    count = await loc.count()
    for k in range(count):
        item = loc.nth(k)
        try:
            if await item.is_visible():
                await item.click(timeout=timeout_ms)
                return
        except Exception:  # noqa: BLE001
            continue
    # None reported visible — let a direct click raise a clear, actionable error.
    await loc.first.click(timeout=timeout_ms)


def _safe_name(name: str) -> str:
    name = (name or "file").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name).lstrip("._")
    return name[:200] or "file"


def _name_from_url(url: str) -> str:
    tail = url.split("?")[0].rstrip("/").split("/")[-1]
    return tail or "download.bin"


def _guess_mime(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"
