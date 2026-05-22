"""Delta eSourcing adapter against a fully faked bridge. No browser, no network.

These exercise the REAL Stage-One flow (corrected in chunk 4c after live recon):
the access code rides in the URL (no form-fill), documents are gated behind a
REGISTER INTEREST button (pause), and each document is a direct downloadDocument
GET (no per-row menu clicks). The Delta-specific selectors/URLs are the constants
the user validates manually.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tender_agent.services.bridge_client import BridgeFile
from tender_agent.services.portals.adapters.delta_esourcing import (
    DELTA_SELECTORS,
    DELTA_URLS,
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

CLOSED_BANNER = "This opportunity is not currently open."
STAGE_ONE_URL = (
    "https://www.delta-esourcing.com/delta/suppliers/select/"
    "suppRespStatus.html?id=555&listId=777"
)


def _docs_table_html(resp_id="555", list_id="777", n=3, titles=None):
    """Build a Stage One documents table with n rows, each carrying a direct
    downloadDocument link (the real download mechanism)."""
    rows = []
    for i in range(n):
        doc_id = 1000 + i
        title = titles[i] if titles else f"Document {i}.pdf"
        href = (
            "/delta/suppliers/response/overview/documents/downloadDocument.html"
            f"?respId={resp_id}&supplierListId={list_id}&docId={doc_id}"
        )
        rows.append(
            f"<tr><td>{title}</td><td>1 MB</td><td>PDF</td><td>01/05/2026</td>"
            f"<td><a href='{href}'>Download File</a></td></tr>"
        )
    return (
        "<table><tr><th>Document Title</th><th>Size</th><th>File Type</th>"
        "<th>Uploaded</th><th>Action</th></tr>" + "".join(rows) + "</table>"
    )


class FakeBridge:
    """Records interactions and returns canned page state. element_exists is
    driven by `present_selectors` (the exact CSS strings the adapter passes);
    find_links filters the configured links by the requested regex."""

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
        self.selects: list[tuple[str, dict]] = []
        self.click_downloads: list[str] = []

    async def bridge_available(self):
        return True

    async def open_session(self, slug, start_url):
        return {}

    async def navigate(self, slug, url):
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

    async def click_download(self, slug, selector, dest_filename=None):
        # Tracked so tests can assert the adapter NEVER uses the menu-click path.
        self.click_downloads.append(selector)
        raise AssertionError("click_download must not be used by the Delta adapter")

    async def select_option(self, slug, selector, value=None, label=None, index=None):
        self.selects.append((selector, {"value": value, "label": label, "index": index}))
        return {"ok": True}

    async def element_exists(self, slug, selector):
        return selector in self.present_selectors

    async def find_links(self, slug, pattern):
        rx = re.compile(pattern, re.IGNORECASE)
        return [u for u in self.links if rx.search(u)]

    async def download(self, slug, url, dest_filename=None):
        safe = re.sub(
            r"[^A-Za-z0-9._-]", "_", dest_filename or url.split("docId=")[-1] or "doc"
        )
        p = Path(self.download_dir) / safe
        p.write_bytes(b"%PDF fake " + url.encode())
        return BridgeFile(
            path=safe, size_bytes=p.stat().st_size, mime_type="application/pdf"
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


def _patch_storage(monkeypatch, tmp_path):
    from tender_agent.services.portals.adapters import delta_esourcing as mod

    bridge_dl = tmp_path / "bridge-dl"
    bridge_dl.mkdir()
    storage = tmp_path / "storage"
    monkeypatch.setattr(mod.settings, "bridge_download_dir", str(bridge_dl))
    monkeypatch.setattr(mod.settings, "document_storage_dir", str(storage))
    return bridge_dl, storage


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


# --- locate: notice page via accessCode URL (no form-fill) -------------


@pytest.mark.asyncio
async def test_locate_opens_notice_via_access_code_url_and_gates_on_register():
    bridge = FakeBridge(text="This tender is currently OPEN. REGISTER INTEREST to bid.")
    res = await DeltaEsourcingAdapter().locate_tender(
        _ctx(bridge, source_url="https://www.delta-esourcing.com/respond/286EVX23TV"),
        "REF-123",
    )
    assert res.status == LocateStatus.requires_interest_first
    assert "Register Interest" in res.detail
    # Navigated straight to the accessCode notice URL — no form-fill at all.
    assert any("respondToList.html?accessCode=286EVX23TV" in u for u in bridge.navigated)
    assert bridge.fills == []
    assert bridge.clicks == []


@pytest.mark.asyncio
async def test_locate_register_gate_via_element_when_text_absent():
    bridge = FakeBridge(
        text="Some notice text without the literal label.",
        present_selectors={DELTA_SELECTORS["register_interest_button"]},
    )
    res = await DeltaEsourcingAdapter().locate_tender(
        _ctx(bridge, source_url="https://www.delta-esourcing.com/respond/286EVX23TV"),
        "REF-123",
    )
    assert res.status == LocateStatus.requires_interest_first


@pytest.mark.asyncio
async def test_locate_closed_tender_returns_not_found_gracefully():
    bridge = FakeBridge(text=f"Notice. {CLOSED_BANNER} Please contact the buyer.")
    res = await DeltaEsourcingAdapter().locate_tender(
        _ctx(bridge, source_url="https://www.delta-esourcing.com/respond/286EVX23TV"),
        "REF-123",
    )
    assert res.status == LocateStatus.not_found
    assert "not currently open" in res.detail
    assert bridge.fills == []


@pytest.mark.asyncio
async def test_locate_already_registered_found_and_captures_ids():
    # No REGISTER INTEREST button; an existing Stage One link is present.
    bridge = FakeBridge(
        text="Your response is in progress. View documents.",
        links=[STAGE_ONE_URL],
    )
    adapter = DeltaEsourcingAdapter()
    res = await adapter.locate_tender(
        _ctx(bridge, source_url="https://www.delta-esourcing.com/respond/286EVX23TV"),
        "REF-123",
    )
    assert res.status == LocateStatus.found
    assert (adapter._resp_id, adapter._list_id) == ("555", "777")


@pytest.mark.asyncio
async def test_locate_no_access_code_returns_not_found():
    bridge = FakeBridge()
    res = await DeltaEsourcingAdapter().locate_tender(
        _ctx(bridge, source_url="https://example.com/x", description="no code"),
        "REF-123",
    )
    assert res.status == LocateStatus.not_found
    assert "access code" in res.detail.lower()
    assert bridge.navigated == []


@pytest.mark.asyncio
async def test_locate_legacy_notice_id_navigates_directly():
    bridge = FakeBridge(text="currently OPEN — REGISTER INTEREST")
    legacy = (
        "https://www.delta-esourcing.com/delta/respondToList.html?noticeId=1032668140"
    )
    res = await DeltaEsourcingAdapter().locate_tender(
        _ctx(bridge, source_url=legacy), "REF-123"
    )
    assert res.status == LocateStatus.requires_interest_first
    assert any("noticeId=1032668140" in u for u in bridge.navigated)
    assert bridge.fills == []


# --- register interest (only ever called after user confirm) -----------


@pytest.mark.asyncio
async def test_register_interest_clicks_and_captures_stage_one_ids():
    # After the click, Delta lands on Stage One — current_url carries the ids.
    bridge = FakeBridge(current_url=STAGE_ONE_URL)
    adapter = DeltaEsourcingAdapter()
    res = await adapter.register_interest(_ctx(bridge))
    assert res.status == RegisterStatus.success
    assert DELTA_SELECTORS["register_interest_button"] in bridge.clicks
    assert (adapter._resp_id, adapter._list_id) == ("555", "777")


# --- download: direct downloadDocument GET (no menu clicks) ------------


@pytest.mark.asyncio
async def test_download_documents_via_direct_links(tmp_path, monkeypatch):
    bridge_dl, storage = _patch_storage(monkeypatch, tmp_path)
    bridge = FakeBridge(
        html=_docs_table_html(n=3),
        current_url=STAGE_ONE_URL,  # resolve respId/listId from here
        download_dir=str(bridge_dl),
    )
    res = await DeltaEsourcingAdapter().download_documents(_ctx(bridge), "ignored")
    assert res.status == DownloadStatus.complete
    assert len(res.files) == 3
    # Navigated to Stage One and built direct download URLs (no menu clicks).
    assert any("suppRespStatus.html?id=555&listId=777" in u for u in bridge.navigated)
    assert bridge.click_downloads == []
    assert bridge.clicks == []
    for f in res.files:
        assert "downloadDocument.html" in f.url
        assert "docId=" in f.url
        assert f.sha256 and f.storage_key
        assert (storage / f.storage_key).is_file()
    # Filenames come from the document titles.
    assert {f.filename for f in res.files} == {"Document_0.pdf", "Document_1.pdf",
                                               "Document_2.pdf"}


@pytest.mark.asyncio
async def test_download_handles_22_documents(tmp_path, monkeypatch):
    bridge_dl, _ = _patch_storage(monkeypatch, tmp_path)
    bridge = FakeBridge(
        html=_docs_table_html(n=22),
        current_url=STAGE_ONE_URL,
        download_dir=str(bridge_dl),
    )
    res = await DeltaEsourcingAdapter().download_documents(_ctx(bridge), "ignored")
    assert res.status == DownloadStatus.complete
    assert len(res.files) == 22


@pytest.mark.asyncio
async def test_download_uses_precaptured_ids_and_maximises_page_size(
    tmp_path, monkeypatch
):
    bridge_dl, _ = _patch_storage(monkeypatch, tmp_path)
    bridge = FakeBridge(
        html=_docs_table_html(resp_id="888", list_id="999", n=2),
        current_url="https://www.delta-esourcing.com/delta/respondToList.html?accessCode=X",
        present_selectors={DELTA_SELECTORS["page_size_select"]},
        download_dir=str(bridge_dl),
    )
    adapter = DeltaEsourcingAdapter()
    adapter._resp_id, adapter._list_id = "888", "999"
    res = await adapter.download_documents(_ctx(bridge), "ignored")
    assert res.status == DownloadStatus.complete
    assert len(res.files) == 2
    assert any("id=888&listId=999" in u for u in bridge.navigated)
    # Page-size dropdown maximised (last option).
    assert bridge.selects and bridge.selects[0][1]["index"] == -1


@pytest.mark.asyncio
async def test_download_caps_at_max_docs(tmp_path, monkeypatch):
    from tender_agent.services.portals.adapters import delta_esourcing as mod

    bridge_dl, _ = _patch_storage(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "MAX_DOCS", 3)
    bridge = FakeBridge(
        html=_docs_table_html(n=5), current_url=STAGE_ONE_URL,
        download_dir=str(bridge_dl),
    )
    res = await DeltaEsourcingAdapter().download_documents(_ctx(bridge), "ignored")
    assert len(res.files) == 3
    assert len(res.missing) == 2
    assert res.status == DownloadStatus.partial


@pytest.mark.asyncio
async def test_download_dedups_identical_files(tmp_path, monkeypatch):
    bridge_dl, _ = _patch_storage(monkeypatch, tmp_path)

    class DupBridge(FakeBridge):
        async def download(self, slug, url, dest_filename=None):
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", dest_filename or "doc")
            p = Path(self.download_dir) / safe
            p.write_bytes(b"%PDF identical")  # same bytes -> same sha256
            return BridgeFile(
                path=safe, size_bytes=p.stat().st_size, mime_type="application/pdf"
            )

    bridge = DupBridge(
        html=_docs_table_html(n=3), current_url=STAGE_ONE_URL,
        download_dir=str(bridge_dl),
    )
    res = await DeltaEsourcingAdapter().download_documents(_ctx(bridge), "ignored")
    assert len(res.files) == 1  # three rows, identical content -> one kept


@pytest.mark.asyncio
async def test_download_errors_without_stage_one_ids(tmp_path, monkeypatch):
    bridge_dl, _ = _patch_storage(monkeypatch, tmp_path)
    bridge = FakeBridge(
        html=_docs_table_html(n=2),
        current_url="https://www.delta-esourcing.com/delta/respondToList.html?accessCode=X",
        download_dir=str(bridge_dl),
    )
    res = await DeltaEsourcingAdapter().download_documents(_ctx(bridge), "ignored")
    assert res.status == DownloadStatus.error
    assert "stage one" in res.detail.lower()


@pytest.mark.asyncio
async def test_download_nothing_available_when_table_empty(tmp_path, monkeypatch):
    bridge_dl, _ = _patch_storage(monkeypatch, tmp_path)
    bridge = FakeBridge(
        html="<table><tr><th>Document Title</th></tr></table>",
        current_url=STAGE_ONE_URL, download_dir=str(bridge_dl),
    )
    res = await DeltaEsourcingAdapter().download_documents(_ctx(bridge), "ignored")
    assert res.status == DownloadStatus.nothing_available


def test_stage_one_and_download_urls_are_well_formed():
    assert DELTA_URLS["stage_one"] % ("555", "777") == STAGE_ONE_URL
    built = DELTA_URLS["document_download"] % ("555", "777", "1000")
    assert "respId=555" in built and "supplierListId=777" in built and "docId=1000" in built
