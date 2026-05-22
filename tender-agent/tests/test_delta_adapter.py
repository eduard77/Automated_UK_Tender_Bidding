"""Delta eSourcing adapter against a fully faked bridge. No browser, no network.

These exercise the REAL Response-Manager flow (corrected from chunk 4):
access-code extraction from the notice, access-code submit, closed-tender
handling, the Express-Interest gate, and authenticated downloads. The
Delta-specific selectors are the constants the user validates manually.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tender_agent.services.bridge_client import BridgeFile
from tender_agent.services.portals.adapters.delta_esourcing import (
    DELTA_SELECTORS,
    DeltaEsourcingAdapter,
    extract_access_code,
)
from tender_agent.services.portals.base import Credentials, PortalContext
from tender_agent.services.portals.results import (
    AuthStatus,
    DownloadStatus,
    LocateStatus,
    RegisterStatus,
)

CLOSED_BANNER = DELTA_SELECTORS["not_open_error_text"]


class FakeBridge:
    """Records interactions and returns canned page state. element_exists is
    driven by `present_selectors` (the exact CSS strings the adapter passes)."""

    def __init__(
        self,
        *,
        text="",
        html="",
        links=None,
        current_url="https://www.delta-esourcing.com/delta/suppliers/select/addToList.html",
        present_selectors=None,
        download_dir=None,
    ):
        self.text = text
        self.html = html
        self.links = links or []
        self.current_url = current_url
        self.present_selectors = set(present_selectors or [])
        self.download_dir = download_dir
        self.navigated: list[str] = []
        self.fills: list[tuple[str, str]] = []
        self.clicks: list[str] = []

    async def bridge_available(self):
        return True

    async def open_session(self, slug, start_url):
        return {}

    async def navigate(self, slug, url):
        # Record the request, but DON'T overwrite current_url: that field models
        # where the session *lands* (e.g. Delta bouncing an unauthenticated user
        # to /login.html), which the test configures via the constructor.
        self.navigated.append(url)
        return {"current_url": self.current_url}

    async def session_status(self, slug):
        return {"current_url": self.current_url, "authenticated_guess": True}

    async def page_text(self, slug):
        return self.text

    async def page_html(self, slug):
        return self.html

    async def fill(self, slug, selector, value):
        self.fills.append((selector, value))
        return {"ok": True}

    async def click(self, slug, selector):
        self.clicks.append(selector)
        return {"ok": True, "current_url": self.current_url}

    async def element_exists(self, slug, selector):
        return selector in self.present_selectors

    async def find_links(self, slug, pattern):
        return self.links

    async def download(self, slug, url, dest_filename=None):
        name = url.split("/")[-1] or "doc.pdf"
        p = Path(self.download_dir) / name
        p.write_bytes(b"%PDF fake " + url.encode())
        return BridgeFile(
            path=name, size_bytes=p.stat().st_size, mime_type="application/pdf"
        )

    async def close_session(self, slug):
        return {"closed": True}


def _ctx(bridge, *, tender_id=900100, candidate_urls=None, source_url=None,
         description=None, tender_ref="REF-123"):
    return PortalContext(
        portal_id=1,
        user_id="tester",
        domain="delta-esourcing.com",
        bridge=bridge,
        platform_slug="delta_esourcing",
        tender_id=tender_id,
        candidate_urls=candidate_urls or [],
        source_url=source_url,
        description=description,
        tender_ref=tender_ref,
    )


# --- access-code extraction (the real URL patterns) --------------------


def test_extract_access_code_respond_pattern():
    assert (
        extract_access_code("https://www.delta-esourcing.com/respond/286EVX23TV")
        == "286EVX23TV"
    )


def test_extract_access_code_tenders_pattern():
    url = "https://www.delta-esourcing.com/tenders/supply-of-widgets/W5C25992M5"
    assert extract_access_code(url) == "W5C25992M5"


def test_extract_access_code_respond_pattern_other_code():
    assert (
        extract_access_code("https://www.delta-esourcing.com/respond/AB3SUXP78A")
        == "AB3SUXP78A"
    )


def test_extract_access_code_legacy_notice_id():
    url = (
        "https://www.delta-esourcing.com/delta/respondToList.html"
        "?noticeId=1032668140"
    )
    assert extract_access_code(url) == "1032668140"


def test_extract_access_code_from_description_text():
    desc = (
        "To respond, register your interest at "
        "https://www.delta-esourcing.com/respond/286EVX23TV before the deadline."
    )
    assert extract_access_code(None, desc) == "286EVX23TV"


def test_extract_access_code_tenders_with_query_and_trailing_text():
    text = "see https://www.delta-esourcing.com/tenders/foo/W5C25992M5?x=1 now"
    assert extract_access_code(text) == "W5C25992M5"


def test_extract_access_code_none_when_no_delta_url():
    assert extract_access_code("https://example.com/x", "no code here", None) is None


def test_extract_access_code_scans_multiple_sources_in_order():
    # source_url has nothing; description carries the code.
    assert (
        extract_access_code(
            "https://www.contractsfinder.service.gov.uk/notice/abc",
            "Delta: https://www.delta-esourcing.com/respond/286EVX23TV",
        )
        == "286EVX23TV"
    )


# --- classification / login --------------------------------------------


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
async def test_is_authenticated_true_when_marker_present():
    bridge = FakeBridge(
        current_url="https://www.delta-esourcing.com/delta/suppliers/select/addToList.html",
        present_selectors={DELTA_SELECTORS["logged_in_marker"]},
    )
    assert await DeltaEsourcingAdapter().is_authenticated(_ctx(bridge)) is True


@pytest.mark.asyncio
async def test_is_authenticated_false_on_login_redirect():
    bridge = FakeBridge(
        current_url="https://www.delta-esourcing.com/delta/login.html",
        present_selectors={DELTA_SELECTORS["logged_in_marker"]},
    )
    assert await DeltaEsourcingAdapter().is_authenticated(_ctx(bridge)) is False


@pytest.mark.asyncio
async def test_is_authenticated_false_without_marker():
    bridge = FakeBridge(present_selectors=set())
    assert await DeltaEsourcingAdapter().is_authenticated(_ctx(bridge)) is False


@pytest.mark.asyncio
async def test_authenticate_is_human_login():
    res = await DeltaEsourcingAdapter().authenticate(_ctx(FakeBridge()), Credentials())
    assert res.status == AuthStatus.success


# --- locate: the access-code submit flow -------------------------------


@pytest.mark.asyncio
async def test_locate_submits_access_code_and_finds_in_responses():
    bridge = FakeBridge(present_selectors={DELTA_SELECTORS["responses_table"]})
    res = await DeltaEsourcingAdapter().locate_tender(
        _ctx(bridge, source_url="https://www.delta-esourcing.com/respond/286EVX23TV"),
        "REF-123",
    )
    assert res.status == LocateStatus.found
    # It filled the access code and clicked submit — never searched.
    assert bridge.fills == [(DELTA_SELECTORS["access_code_input"], "286EVX23TV")]
    assert DELTA_SELECTORS["submit_button"] in bridge.clicks
    assert any("addToList.html" in u for u in bridge.navigated)


@pytest.mark.asyncio
async def test_locate_closed_tender_returns_not_found_gracefully():
    bridge = FakeBridge(text=f"Notice. {CLOSED_BANNER} Please contact the buyer.")
    res = await DeltaEsourcingAdapter().locate_tender(
        _ctx(bridge, source_url="https://www.delta-esourcing.com/respond/286EVX23TV"),
        "REF-123",
    )
    assert res.status == LocateStatus.not_found
    assert "not currently open" in res.detail


@pytest.mark.asyncio
async def test_locate_express_interest_gate_pauses():
    bridge = FakeBridge(
        present_selectors={DELTA_SELECTORS["express_interest_button"]}
    )
    res = await DeltaEsourcingAdapter().locate_tender(
        _ctx(bridge, source_url="https://www.delta-esourcing.com/respond/286EVX23TV"),
        "REF-123",
    )
    assert res.status == LocateStatus.requires_interest_first
    assert "Express Interest" in res.detail


@pytest.mark.asyncio
async def test_locate_no_access_code_returns_not_found():
    bridge = FakeBridge()
    res = await DeltaEsourcingAdapter().locate_tender(
        _ctx(bridge, source_url="https://example.com/x", description="no code"),
        "REF-123",
    )
    assert res.status == LocateStatus.not_found
    assert "access code" in res.detail.lower()
    # Never navigated/filled when there's no code to submit.
    assert bridge.fills == []


@pytest.mark.asyncio
async def test_locate_legacy_notice_id_navigates_directly():
    bridge = FakeBridge(present_selectors={DELTA_SELECTORS["responses_table"]})
    legacy = (
        "https://www.delta-esourcing.com/delta/respondToList.html?noticeId=1032668140"
    )
    res = await DeltaEsourcingAdapter().locate_tender(
        _ctx(bridge, source_url=legacy), "REF-123"
    )
    assert res.status == LocateStatus.found
    # Legacy path navigates straight to the respond URL; no access-code box.
    assert any("noticeId=1032668140" in u for u in bridge.navigated)
    assert bridge.fills == []


# --- express interest (only ever called after user confirm) ------------


@pytest.mark.asyncio
async def test_register_interest_clicks_button():
    bridge = FakeBridge()
    res = await DeltaEsourcingAdapter().register_interest(_ctx(bridge))
    assert res.status == RegisterStatus.success
    assert DELTA_SELECTORS["express_interest_button"] in bridge.clicks


# --- download ----------------------------------------------------------


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
async def test_download_dedups_identical_files(tmp_path, monkeypatch):
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

    class DupBridge(FakeBridge):
        async def download(self, slug, url, dest_filename=None):
            # Same bytes regardless of URL → same sha256 → deduped.
            name = url.split("/")[-1]
            p = Path(self.download_dir) / name
            p.write_bytes(b"%PDF identical")
            return BridgeFile(
                path=name, size_bytes=p.stat().st_size, mime_type="application/pdf"
            )

    bridge = DupBridge(
        links=[
            "https://www.delta-esourcing.com/download/a.pdf",
            "https://www.delta-esourcing.com/download/b.pdf",
        ],
        download_dir=str(bridge_dl),
    )
    res = await DeltaEsourcingAdapter().download_documents(_ctx(bridge), "ignored")
    # Two links, identical content → one file kept after sha256 dedup.
    assert len(res.files) == 1


@pytest.mark.asyncio
async def test_download_caps_at_max_docs(tmp_path, monkeypatch):
    from tender_agent.services.portals.adapters import delta_esourcing as mod

    bridge_dl = tmp_path / "bridge-dl"
    bridge_dl.mkdir()
    storage = tmp_path / "storage"
    monkeypatch.setattr(mod.settings, "bridge_download_dir", str(bridge_dl))
    monkeypatch.setattr(mod.settings, "document_storage_dir", str(storage))
    monkeypatch.setattr(mod, "MAX_DOCS", 3)
    links = [
        f"https://www.delta-esourcing.com/download/doc{i}.pdf" for i in range(5)
    ]
    bridge = FakeBridge(links=links, download_dir=str(bridge_dl))
    res = await DeltaEsourcingAdapter().download_documents(_ctx(bridge), "ignored")
    assert len(res.files) == 3
    assert len(res.missing) == 2  # the 2 over the cap
    assert res.status == DownloadStatus.partial


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
