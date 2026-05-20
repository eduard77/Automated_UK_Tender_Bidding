"""ContractsFinderDirectAdapter — download, security boundary, caps, filename
sanitisation. httpx is mocked via MockTransport; no real network.
"""
from __future__ import annotations

import httpx
import pytest

from tender_agent.services.portals.base import Credentials, PortalContext
from tender_agent.services.portals.contracts_finder import (
    ContractsFinderDirectAdapter,
    secure_filename,
)
from tender_agent.services.portals.results import (
    AuthStatus,
    DownloadStatus,
    RegisterStatus,
)

ASSET = "https://assets.publishing.service.gov.uk/media/abc/itt.pdf"
PORTAL_URL = "https://procontract.due-north.com/Advert?advertId=123"


def _ctx(urls, client=None, tmp=None):
    return PortalContext(
        portal_id=1,
        user_id="tester",
        domain="assets.publishing.service.gov.uk",
        http=client,
        candidate_urls=urls,
        tender_id=999001,
    )


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- secure_filename ----------------------------------------------------


def test_secure_filename_strips_path_and_dots():
    assert secure_filename("../../etc/passwd") == "passwd"
    assert secure_filename("..\\..\\windows\\system32\\evil.dll") == "evil.dll"
    assert secure_filename(".hidden") == "hidden"
    assert secure_filename("a b/c?.pdf") == "c_.pdf"
    assert secure_filename("") == "document"


def test_matches_url_only_asset_host():
    a = ContractsFinderDirectAdapter()
    assert a.matches_url(ASSET) is True
    assert a.matches_url("https://sub.assets.publishing.service.gov.uk/x.pdf") is True
    assert a.matches_url(PORTAL_URL) is False
    assert a.matches_url("https://contractsfinder.service.gov.uk/Notice/x") is False


@pytest.mark.asyncio
async def test_authenticate_and_register(tmp_path):
    a = ContractsFinderDirectAdapter()
    auth = await a.authenticate(_ctx([]), Credentials())
    assert auth.status == AuthStatus.success
    reg = await a.register_interest(_ctx([]))
    assert reg.status == RegisterStatus.error


@pytest.mark.asyncio
async def test_download_real_asset(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tender_agent.services.portals.contracts_finder.settings.document_storage_dir",
        str(tmp_path),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"%PDF-1.5 hello", headers={"content-type": "application/pdf"}
        )

    client = _client(handler)
    res = await ContractsFinderDirectAdapter().download_documents(
        _ctx([ASSET], client=client), str(tmp_path)
    )
    await client.aclose()
    assert res.status == DownloadStatus.complete
    assert len(res.files) == 1
    f = res.files[0]
    assert f.sha256 is not None
    assert f.storage_key is not None
    assert f.bytes == len(b"%PDF-1.5 hello")
    # File actually written under the storage root.
    assert (tmp_path / f.storage_key).is_file()


@pytest.mark.asyncio
async def test_rejects_untrusted_urls(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tender_agent.services.portals.contracts_finder.settings.document_storage_dir",
        str(tmp_path),
    )
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        return httpx.Response(200, content=b"x")

    client = _client(handler)
    res = await ContractsFinderDirectAdapter().download_documents(
        _ctx([PORTAL_URL], client=client), str(tmp_path)
    )
    await client.aclose()
    # Untrusted URL must never be fetched.
    assert fetched == []
    assert res.files == []
    assert PORTAL_URL in res.missing
    # No allowed URLs at all -> complete-but-empty (CF authoritative).
    assert res.status == DownloadStatus.complete


@pytest.mark.asyncio
async def test_oversize_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tender_agent.services.portals.contracts_finder.settings.document_storage_dir",
        str(tmp_path),
    )
    monkeypatch.setattr(
        "tender_agent.services.portals.contracts_finder.MAX_FILE_BYTES", 10
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 50)

    client = _client(handler)
    res = await ContractsFinderDirectAdapter().download_documents(
        _ctx([ASSET], client=client), str(tmp_path)
    )
    await client.aclose()
    assert res.files == []
    assert ASSET in res.missing


@pytest.mark.asyncio
async def test_doc_cap_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tender_agent.services.portals.contracts_finder.settings.document_storage_dir",
        str(tmp_path),
    )
    monkeypatch.setattr(
        "tender_agent.services.portals.contracts_finder.MAX_DOCS", 2
    )
    urls = [f"https://assets.publishing.service.gov.uk/m/{i}.pdf" for i in range(5)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"%PDF data", headers={"content-type": "application/pdf"}
        )

    client = _client(handler)
    res = await ContractsFinderDirectAdapter().download_documents(
        _ctx(urls, client=client), str(tmp_path)
    )
    await client.aclose()
    assert len(res.files) == 2  # capped
    assert len(res.missing) == 3  # the over-cap remainder


@pytest.mark.asyncio
async def test_partial_when_some_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tender_agent.services.portals.contracts_finder.settings.document_storage_dir",
        str(tmp_path),
    )
    good = "https://assets.publishing.service.gov.uk/m/good.pdf"
    bad = "https://assets.publishing.service.gov.uk/m/bad.pdf"

    def handler(request: httpx.Request) -> httpx.Response:
        if "bad" in request.url.path:
            return httpx.Response(404, content=b"nope")
        return httpx.Response(200, content=b"%PDF ok", headers={"content-type": "application/pdf"})

    client = _client(handler)
    res = await ContractsFinderDirectAdapter().download_documents(
        _ctx([good, bad], client=client), str(tmp_path)
    )
    await client.aclose()
    assert res.status == DownloadStatus.partial
    assert len(res.files) == 1
    assert bad in res.missing
