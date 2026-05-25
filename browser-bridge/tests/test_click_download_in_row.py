"""Unit tests for the /click-download-in-row endpoint — no real browser.

The endpoint opens a table row's ⋮ action menu and clicks its "Download File"
item, capturing the resulting Playwright download. These inject a fake page into
the session manager so the row-resolution (header/empty rows skipped), the
download capture, the no-download timeout, and the token check are all covered
without a browser or network.
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


class FakeTrigger:
    """A row's ⋮ action-menu control."""

    def __init__(self) -> None:
        self.clicks = 0

    async def click(self) -> None:
        self.clicks += 1


class FakeRow:
    """A table row. `is_data` mirrors a real data row (has a <td>); header rows
    have only <th> so query_selector('td') returns None. `trigger` is the ⋮
    control returned for the menu-trigger selector."""

    def __init__(self, *, is_data: bool, trigger: FakeTrigger | None = None) -> None:
        self._is_data = is_data
        self.trigger = trigger

    async def query_selector(self, selector: str):
        if selector == "td":
            return object() if self._is_data else None
        return self.trigger


class FakeDownload:
    def __init__(self, suggested: str = "report.pdf", content: bytes = b"%PDF data"):
        self.suggested_filename = suggested
        self._content = content

    async def save_as(self, path: str) -> None:
        Path(path).write_bytes(self._content)


class _ExpectDownload:
    """Stand-in for page.expect_download(): an async context manager whose
    awaitable `.value` yields the download (or raises if none fires)."""

    def __init__(self, download: FakeDownload, *, fire: bool) -> None:
        self._download = download
        self._fire = fire

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def value(self):
        download, fire = self._download, self._fire

        async def _get():
            if not fire:
                raise TimeoutError("Timeout exceeded waiting for download")
            return download

        return _get()


class FakeLocator:
    """Stand-in for page.get_by_text(...).last — records the clicked text."""

    def __init__(self, page: FakePage, text: str) -> None:
        self._page = page
        self._text = text

    @property
    def last(self) -> FakeLocator:
        return self

    async def click(self, timeout: int | None = None) -> None:
        self._page.clicked.append(f"get_by_text:{self._text}")


class FakePage:
    def __init__(
        self,
        rows: list[FakeRow],
        *,
        download: FakeDownload | None = None,
        fire_download: bool = True,
        url: str = "https://www.delta-esourcing.com/delta/x",
    ) -> None:
        self._rows = rows
        self._download = download or FakeDownload()
        self._fire = fire_download
        self.url = url
        self.query_selector_all_arg: str | None = None
        self.clicked: list[str] = []

    async def query_selector_all(self, selector: str):
        self.query_selector_all_arg = selector
        return self._rows

    def expect_download(self, timeout: int | None = None):
        return _ExpectDownload(self._download, fire=self._fire)

    def get_by_text(self, text: str, exact: bool = False) -> FakeLocator:
        return FakeLocator(self, text)


@pytest.fixture()
def client_and_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("TENDER_AGENT_BRIDGE_DOWNLOAD_DIR", str(tmp_path / "dl"))
    from bridge.server import app, manager

    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://bridge.test")
    return client, manager


async def _post(client, manager, page, body, *, headers=HEADERS):
    manager._sessions["delta"] = SimpleNamespace(page=page)
    try:
        return await client.post(
            "/session/delta/click-download-in-row", json=body, headers=headers
        )
    finally:
        manager._sessions.pop("delta", None)
        await client.aclose()


def _body(**over):
    body = {
        "table_selector": "table#document",
        "row_index": 0,
        "menu_trigger_selector": "i.bip-actions-menu-link.fa-ellipsis-v",
        "download_item_text": "Download File",
    }
    body.update(over)
    return body


@pytest.mark.asyncio
async def test_captures_download_and_returns_path(client_and_manager):
    client, manager = client_and_manager
    trigger = FakeTrigger()
    rows = [
        FakeRow(is_data=False),  # header row (only <th>) — skipped
        FakeRow(is_data=True, trigger=trigger),  # data row 0
    ]
    page = FakePage(rows, download=FakeDownload(suggested="spec.pdf"))
    resp = await _post(client, manager, page, _body(row_index=0))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["suggested_filename"] == "spec.pdf"
    assert data["path"].endswith("spec.pdf")
    assert data["size_bytes"] > 0
    # The header row was skipped, so data-row 0 is the row that got its ⋮ clicked.
    assert trigger.clicks == 1
    # The "Download File" item was clicked page-wide by text (get_by_text .last).
    assert page.clicked == ["get_by_text:Download File"]
    # The captured file landed in the configured bridge download dir.
    from bridge import config

    assert (config.download_dir() / data["path"]).is_file()


@pytest.mark.asyncio
async def test_header_and_empty_rows_skipped_for_row_index(client_and_manager):
    client, manager = client_and_manager
    t0, t1 = FakeTrigger(), FakeTrigger()
    rows = [
        FakeRow(is_data=False),  # header
        FakeRow(is_data=True, trigger=t0),  # data row 0
        FakeRow(is_data=False),  # an empty/non-data row (no <td>)
        FakeRow(is_data=True, trigger=t1),  # data row 1
    ]
    page = FakePage(rows)
    resp = await _post(client, manager, page, _body(row_index=1))
    assert resp.status_code == 200, resp.text
    # row_index=1 maps to the SECOND data row, not the second <tr>.
    assert t0.clicks == 0
    assert t1.clicks == 1
    assert page.query_selector_all_arg == "table#document tr"


@pytest.mark.asyncio
async def test_no_download_event_returns_502(client_and_manager):
    client, manager = client_and_manager
    rows = [FakeRow(is_data=True, trigger=FakeTrigger())]
    page = FakePage(rows, fire_download=False)
    resp = await _post(client, manager, page, _body(row_index=0))
    assert resp.status_code == 502
    assert "no download captured" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_row_index_out_of_range_returns_502(client_and_manager):
    client, manager = client_and_manager
    rows = [FakeRow(is_data=True, trigger=FakeTrigger())]
    page = FakePage(rows)
    resp = await _post(client, manager, page, _body(row_index=5))
    assert resp.status_code == 502
    assert "out of range" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_missing_menu_trigger_returns_502(client_and_manager):
    client, manager = client_and_manager
    rows = [FakeRow(is_data=True, trigger=None)]  # no ⋮ control found in row
    page = FakePage(rows)
    resp = await _post(client, manager, page, _body(row_index=0))
    assert resp.status_code == 502
    assert "trigger not found" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_requires_token(client_and_manager):
    client, manager = client_and_manager
    rows = [FakeRow(is_data=True, trigger=FakeTrigger())]
    page = FakePage(rows)
    resp = await _post(
        client, manager, page, _body(), headers={"X-Bridge-Token": "wrong"}
    )
    assert resp.status_code == 401
