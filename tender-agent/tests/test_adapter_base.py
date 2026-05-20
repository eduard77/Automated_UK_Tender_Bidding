"""FallbackAdapter behaviour. No real network — httpx is mocked via MockTransport."""
from __future__ import annotations

import httpx
import pytest

from tender_agent.services.portals.base import Credentials, PortalContext
from tender_agent.services.portals.fallback import FallbackAdapter
from tender_agent.services.portals.results import (
    AuthStatus,
    DownloadStatus,
    LocateStatus,
    RegisterStatus,
)


def _ctx(candidate_urls: list[str], client: httpx.AsyncClient | None = None) -> PortalContext:
    return PortalContext(
        portal_id=1,
        user_id="tester",
        domain="example.com",
        http=client,
        candidate_urls=candidate_urls,
    )


def test_matches_url_is_catch_all() -> None:
    assert FallbackAdapter().matches_url("https://anything.example") is True


@pytest.mark.asyncio
async def test_authenticate_always_succeeds() -> None:
    res = await FallbackAdapter().authenticate(_ctx([]), Credentials())
    assert res.status == AuthStatus.success


@pytest.mark.asyncio
async def test_locate_returns_found() -> None:
    res = await FallbackAdapter().locate_tender(_ctx([]), "ref-1")
    assert res.status == LocateStatus.found


@pytest.mark.asyncio
async def test_register_interest_always_errors() -> None:
    res = await FallbackAdapter().register_interest(_ctx([]))
    assert res.status == RegisterStatus.error


@pytest.mark.asyncio
async def test_download_nothing_when_no_urls(tmp_path) -> None:
    res = await FallbackAdapter().download_documents(_ctx([]), str(tmp_path))
    assert res.status == DownloadStatus.nothing_available


@pytest.mark.asyncio
async def test_download_complete_when_public(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"%PDF-1.4 fake",
            headers={"content-type": "application/pdf"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ctx = _ctx(["https://example.com/itt.pdf"], client=client)
    res = await FallbackAdapter().download_documents(ctx, str(tmp_path))
    await client.aclose()
    assert res.status == DownloadStatus.complete
    assert len(res.files) == 1
    assert res.files[0].filename == "itt.pdf"


@pytest.mark.asyncio
async def test_download_partial_when_some_gated(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "gated" in request.url.path:
            return httpx.Response(403, content=b"denied")
        return httpx.Response(
            200, content=b"data", headers={"content-type": "application/octet-stream"}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ctx = _ctx(
        ["https://example.com/open.bin", "https://example.com/gated.bin"],
        client=client,
    )
    res = await FallbackAdapter().download_documents(ctx, str(tmp_path))
    await client.aclose()
    assert res.status == DownloadStatus.partial
    assert len(res.files) == 1
    assert res.missing == ["https://example.com/gated.bin"]


@pytest.mark.asyncio
async def test_download_treats_login_html_as_gated(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html><body>Please sign in with your password</body></html>",
            headers={"content-type": "text/html"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ctx = _ctx(["https://example.com/itt"], client=client)
    res = await FallbackAdapter().download_documents(ctx, str(tmp_path))
    await client.aclose()
    assert res.status == DownloadStatus.nothing_available
    assert res.missing == ["https://example.com/itt"]
