"""Delta eSourcing adapter against a fully faked bridge. No browser, no network.

These exercise the control flow; the Delta-specific selectors/URLs are the
constants validated manually on Friday.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tender_agent.services.bridge_client import BridgeFile
from tender_agent.services.portals.adapters.delta_esourcing import (
    DeltaEsourcingAdapter,
)
from tender_agent.services.portals.base import Credentials, PortalContext
from tender_agent.services.portals.results import (
    AuthStatus,
    DownloadStatus,
    LocateStatus,
    RegisterStatus,
)


class FakeBridge:
    def __init__(self, *, html="", links=None, current_url="", download_dir=None):
        self.html = html
        self.links = links or []
        self.current_url = current_url
        self.download_dir = download_dir
        self.navigated: list[str] = []

    async def bridge_available(self):
        return True

    async def open_session(self, slug, start_url):
        return {}

    async def navigate(self, slug, url):
        self.navigated.append(url)
        self.current_url = url
        return {"current_url": url}

    async def session_status(self, slug):
        return {"current_url": self.current_url, "authenticated_guess": True}

    async def page_html(self, slug):
        return self.html

    async def find_links(self, slug, pattern):
        return self.links

    async def download(self, slug, url, dest_filename=None):
        name = url.split("/")[-1] or "doc.pdf"
        p = Path(self.download_dir) / name
        p.write_bytes(b"%PDF fake " + url.encode())
        return BridgeFile(path=name, size_bytes=p.stat().st_size, mime_type="application/pdf")

    async def close_session(self, slug):
        return {"closed": True}


def _ctx(bridge, *, tender_id=900100, candidate_urls=None, source_url=None):
    return PortalContext(
        portal_id=1,
        user_id="tester",
        domain="delta-esourcing.com",
        bridge=bridge,
        platform_slug="delta_esourcing",
        tender_id=tender_id,
        candidate_urls=candidate_urls or [],
        source_url=source_url,
        tender_ref="REF-123",
    )


def test_matches_url():
    a = DeltaEsourcingAdapter()
    assert a.matches_url("https://www.delta-esourcing.com/delta/x") is True
    assert a.matches_url("https://nlb.delta-esourcing.com/y") is True
    assert a.matches_url("https://procontract.due-north.com/z") is False


def test_requires_login_and_login_url():
    a = DeltaEsourcingAdapter()
    assert a.requires_login is True
    assert "delta-esourcing.com" in a.login_url()


@pytest.mark.asyncio
async def test_is_authenticated_true():
    bridge = FakeBridge(html="<a href='/logout'>Logout</a>", current_url="https://www.delta-esourcing.com/delta/respond.html")
    assert await DeltaEsourcingAdapter().is_authenticated(_ctx(bridge)) is True


@pytest.mark.asyncio
async def test_is_authenticated_false_on_login_redirect():
    bridge = FakeBridge(html="<input type=password>", current_url="https://www.delta-esourcing.com/delta/login.html")
    assert await DeltaEsourcingAdapter().is_authenticated(_ctx(bridge)) is False


@pytest.mark.asyncio
async def test_authenticate_is_human_login():
    res = await DeltaEsourcingAdapter().authenticate(_ctx(FakeBridge()), Credentials())
    assert res.status == AuthStatus.success


@pytest.mark.asyncio
async def test_locate_tender_express_interest_gate():
    bridge = FakeBridge(html="<div>Please Express Interest to view documents</div>")
    res = await DeltaEsourcingAdapter().locate_tender(
        _ctx(bridge, source_url="https://www.delta-esourcing.com/delta/tender/1"), "REF-123"
    )
    assert res.status == LocateStatus.requires_interest_first


@pytest.mark.asyncio
async def test_locate_tender_found_when_docs_present():
    bridge = FakeBridge(html="<a href='https://www.delta-esourcing.com/download/1.pdf'>doc</a>")
    res = await DeltaEsourcingAdapter().locate_tender(
        _ctx(bridge, source_url="https://www.delta-esourcing.com/delta/tender/1"), "REF-123"
    )
    assert res.status == LocateStatus.found


@pytest.mark.asyncio
async def test_locate_refuses_off_platform():
    bridge = FakeBridge(html="x")
    await DeltaEsourcingAdapter().locate_tender(
        _ctx(bridge, source_url="https://evil.example/tender/1"), "REF-123"
    )
    # off-platform source_url is ignored; falls back to the Delta search URL,
    # which IS on-platform, so it navigates there (found/empty), never to evil.
    assert "evil.example" not in " ".join(bridge.navigated)


@pytest.mark.asyncio
async def test_register_interest_clicks(monkeypatch):
    bridge = FakeBridge()

    async def fake_click(slug, selector, dest_filename=None):
        raise RuntimeError("not a download")  # express interest isn't a download

    bridge.click_download = fake_click
    res = await DeltaEsourcingAdapter().register_interest(_ctx(bridge))
    assert res.status == RegisterStatus.success


@pytest.mark.asyncio
async def test_download_documents_persists(tmp_path, monkeypatch):
    bridge_dl = tmp_path / "bridge-dl"
    bridge_dl.mkdir()
    storage = tmp_path / "storage"
    monkeypatch.setattr(
        "tender_agent.services.portals.adapters.delta_esourcing.settings.bridge_download_dir",
        str(bridge_dl),
    )
    monkeypatch.setattr(
        "tender_agent.services.portals.adapters.delta_esourcing.settings.document_storage_dir",
        str(storage),
    )
    bridge = FakeBridge(
        links=["https://www.delta-esourcing.com/download/itt.pdf"],
        download_dir=str(bridge_dl),
    )
    res = await DeltaEsourcingAdapter().download_documents(_ctx(bridge), "ignored")
    assert res.status == DownloadStatus.complete
    assert len(res.files) == 1
    f = res.files[0]
    assert f.sha256 and f.storage_key
    assert (storage / f.storage_key).is_file()


@pytest.mark.asyncio
async def test_download_rejects_off_platform_links(tmp_path, monkeypatch):
    bridge_dl = tmp_path / "bridge-dl"
    bridge_dl.mkdir()
    monkeypatch.setattr(
        "tender_agent.services.portals.adapters.delta_esourcing.settings.bridge_download_dir",
        str(bridge_dl),
    )
    bridge = FakeBridge(
        links=["https://evil.example/x.pdf"], download_dir=str(bridge_dl)
    )
    res = await DeltaEsourcingAdapter().download_documents(_ctx(bridge), "ignored")
    assert res.status == DownloadStatus.nothing_available
    assert "https://evil.example/x.pdf" in res.missing
