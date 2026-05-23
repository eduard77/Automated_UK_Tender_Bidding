"""Unit tests for /click-download-in-row — no real browser.

The endpoint opens a row's action menu and clicks "Download File", capturing the
resulting Playwright download. These inject a fake page into the session manager
so the row-indexing, visible-item click, capture/save, and error paths are
covered without a browser or network.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

os.environ.setdefault("TENDER_AGENT_BRIDGE_TOKEN", "test")
TOKEN = os.environ["TENDER_AGENT_BRIDGE_TOKEN"]
HEADERS = {"X-Bridge-Token": TOKEN}

ROWS = "table#document tbody tr"
TRIGGER = "button.dots"
ITEM = "a:has-text('Download File')"


class FakeDownload:
    def __init__(self, suggested_filename: str, content: bytes = b"%PDF data"):
        self.suggested_filename = suggested_filename
        self._content = content

    async def save_as(self, path: str) -> None:
        Path(path).write_bytes(self._content)


class _ExpectDownloadCtx:
    """Mimics page.expect_download(): `await dl_info.value` yields the download,
    or raises if none was produced (the timeout case)."""

    def __init__(self, download):
        self._download = download

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def value(self):
        download = self._download

        async def _get():
            if download is None:
                raise TimeoutError("no download event within timeout")
            return download

        return _get()


class FakeLocator:
    def __init__(self, page, *, kind, count, row_index=None):
        self.page = page
        self.kind = kind
        self._count = count
        self.row_index = row_index

    async def count(self):
        return self._count

    def nth(self, k):
        if self.kind == "rows":
            return FakeLocator(self.page, kind="row", count=1, row_index=k)
        return FakeLocator(self.page, kind=f"{self.kind}-one", count=1, row_index=k)

    @property
    def first(self):
        return self

    def locator(self, selector):
        if self.kind == "row" and selector == self.page.trigger_selector:
            has = self.page.row_has_trigger(self.row_index)
            return FakeLocator(
                self.page, kind="trigger", count=1 if has else 0,
                row_index=self.row_index,
            )
        return FakeLocator(self.page, kind="other", count=0, row_index=self.row_index)

    async def is_visible(self):
        return True

    async def click(self, timeout=None):
        self.page.clicks.append((self.kind, self.row_index))


class FakePage:
    def __init__(
        self, *, n_data_rows, download, item_count=1, header_row=False,
        rows_selector=ROWS, trigger_selector=TRIGGER, item_selector=ITEM,
    ):
        self.n_data_rows = n_data_rows
        self.download = download
        self.item_count = item_count
        self.header_row = header_row
        self.rows_selector = rows_selector
        self.trigger_selector = trigger_selector
        self.item_selector = item_selector
        self.clicks: list = []
        self.url = "https://www.delta-esourcing.com/delta/stageone"

    def row_has_trigger(self, k):
        # A header row (when present) sits at index 0 and has no action trigger.
        return not (self.header_row and k == 0)

    def locator(self, selector):
        if selector == self.rows_selector:
            total = self.n_data_rows + (1 if self.header_row else 0)
            return FakeLocator(self, kind="rows", count=total)
        if selector == self.item_selector:
            return FakeLocator(self, kind="item", count=self.item_count)
        return FakeLocator(self, kind="other", count=0)

    def expect_download(self, timeout=None):
        return _ExpectDownloadCtx(self.download)


@pytest.fixture()
def client_and_manager():
    from bridge.server import app, manager

    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://bridge.test")
    return client, manager


async def _post(client, manager, page, body):
    manager._sessions["delta"] = SimpleNamespace(page=page)
    try:
        return await client.post(
            "/session/delta/click-download-in-row", json=body, headers=HEADERS
        )
    finally:
        manager._sessions.pop("delta", None)
        await client.aclose()


def _body(index=0):
    return {
        "rows_selector": ROWS, "trigger_selector": TRIGGER, "item_selector": ITEM,
        "index": index, "timeout_ms": 5000,
    }


@pytest.mark.asyncio
async def test_click_download_in_row_captures_file(client_and_manager):
    client, manager = client_and_manager
    page = FakePage(n_data_rows=3, download=FakeDownload("spec.pdf", b"%PDF body"))
    resp = await _post(client, manager, page, _body(index=1))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["suggested_filename"] == "spec.pdf"
    assert data["size_bytes"] == len(b"%PDF body")

    from bridge import config

    saved = config.download_dir() / data["path"]
    assert saved.is_file()
    # Opened the row's action menu (trigger) then clicked the Download File item.
    assert ("trigger", 1) in page.clicks
    assert any(kind.startswith("item") for kind, _ in page.clicks)


@pytest.mark.asyncio
async def test_click_download_in_row_skips_header_row_for_index(client_and_manager):
    # A header row sits in <tbody>; index 0 must map to the first DATA row.
    client, manager = client_and_manager
    page = FakePage(
        n_data_rows=2, header_row=True, download=FakeDownload("d.pdf")
    )
    resp = await _post(client, manager, page, _body(index=0))
    assert resp.status_code == 200, resp.text
    # The clicked trigger was the data row at DOM index 1 (0 is the header).
    assert ("trigger", 1) in page.clicks


@pytest.mark.asyncio
async def test_click_download_in_row_no_event_is_clear_error(client_and_manager):
    client, manager = client_and_manager
    page = FakePage(n_data_rows=2, download=None)  # no download fires
    resp = await _post(client, manager, page, _body(index=0))
    assert resp.status_code == 502
    assert "click-download-in-row failed" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_click_download_in_row_index_out_of_range(client_and_manager):
    client, manager = client_and_manager
    page = FakePage(n_data_rows=2, download=FakeDownload("d.pdf"))
    resp = await _post(client, manager, page, _body(index=5))
    assert resp.status_code == 404
    assert "out of range" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_click_download_in_row_requires_token(client_and_manager):
    client, manager = client_and_manager
    page = FakePage(n_data_rows=1, download=FakeDownload("d.pdf"))
    manager._sessions["delta"] = SimpleNamespace(page=page)
    try:
        resp = await client.post(
            "/session/delta/click-download-in-row",
            json=_body(index=0),
            headers={"X-Bridge-Token": "wrong"},
        )
        assert resp.status_code == 401
    finally:
        manager._sessions.pop("delta", None)
        await client.aclose()
