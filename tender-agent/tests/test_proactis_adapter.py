"""Proactis / ProContract adapter against a fully faked bridge. No browser, no
network.

These exercise the confirmed flow: login (no 2FA, human in the bridge window),
locate by rfxId or My-activities title match, the express-interest branches
(already-interested no-pause, immediate release, email-confirmation awaiting
state + resume), and document fetch from BOTH activity sections (the Activity
documentation table AND the Terms & conditions list) via a direct-link primary
with a click fallback. Files flow through the SHARED _persist_documents +
ensure_content_extracted (DB-backed), never a Proactis-specific path.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.db import engine
from tender_agent.models import (
    Tender,
    TenderDocumentContent,
    TenderDocumentFile,
)
from tender_agent.services.bridge_client import BridgeError, BridgeFile, RenderedPage
from tender_agent.services.portal_orchestrator import PortalOrchestrator
from tender_agent.services.portals.adapters.proactis import (
    ALREADY_INTERESTED_DETAIL,
    AWAITING_BUYER_RELEASE_DETAIL,
    EXPRESS_INTEREST_DETAIL,
    PROACTIS_SELECTORS,
    PROACTIS_URLS,
    ProactisAdapter,
    extract_rfx_id,
)
from tender_agent.services.portals.base import Credentials, PortalContext
from tender_agent.services.portals.results import (
    AuthStatus,
    DownloadStatus,
    LocateStatus,
    RegisterStatus,
)

RFX = "11112222-3333-4444-5555-666677778888"
ACTIVITY_URL = PROACTIS_URLS["activity"] % RFX
SOURCE_URL = f"https://procontract.due-north.com/RfxResponse?rfxId={RFX}"


# --- activity-page fixtures ---------------------------------------------


def _activity_html(doc_files, terms_files, *, email=False):
    """Build an activity (RfxResponse) page with TWO document areas mirroring the
    live layout: an "Activity documentation, files & links (N)" table whose Title
    column is a link, and a SEPARATE "Terms & conditions (M)" list of links."""
    doc_rows = "".join(
        f"<tr><td><a href='{href}'>{title}</a></td><td>{(title.rsplit('.', 1)[-1]).upper()}</td>"
        "<td>127 KB</td></tr>"
        for title, href in doc_files
    )
    terms_items = "".join(
        f"<li><a href='{href}'>{title}</a></li>" for title, href in terms_files
    )
    email_block = (
        "<div class='alert'>Your expression of interest has been received. "
        "You will receive an email; once the buyer confirms, documents are "
        "released.</div>"
        if email
        else ""
    )
    return (
        "<html><body>"
        "<h1>Activity</h1>"
        "<div>Buyer: Knowsley Council</div>"
        "<div>Deadline: A response must be submitted no later than 30/06/2026</div>"
        f"{email_block}"
        f"<h2>Activity documentation, files &amp; links ({len(doc_files)})</h2>"
        "<table id='activityDocuments'><thead><tr><th>Title</th><th>Type</th>"
        f"<th>Size</th></tr></thead><tbody>{doc_rows}</tbody></table>"
        f"<h2>Terms &amp; conditions ({len(terms_files)})</h2>"
        f"<ul class='terms-and-conditions'>{terms_items}</ul>"
        "<h2>Clarifications (0)</h2>"
        "<div class='footer'>Powered by Proactis</div>"
        "</body></html>"
    )


def _knowsley_activity(email=False):
    """The manual-test tender shape: 3 documentation files + 1 terms file."""
    docs = [
        ("Section 1 Exec Summary and Specification v6.docx", "/Download/Attachment/d1"),
        ("Pricing Schedule.xlsx", "/Download/Attachment/d2"),
        ("DPS Member Agreement.pdf", "/Download/Attachment/d3"),
    ]
    terms = [("Services (without TUPE)", "/Download/Terms/t1")]
    return _activity_html(docs, terms, email=email)


def _activities_listing(rows):
    """My-activities listing: each activity is a link to RfxResponse?rfxId=…."""
    items = "".join(
        f"<li><a href='/RfxResponse?rfxId={rfx}'>{title}</a></li>"
        for rfx, title in rows
    )
    return f"<html><body><h1>My activities</h1><ul>{items}</ul></body></html>"


# --- fake bridge --------------------------------------------------------


class FakeBridge:
    def __init__(
        self,
        *,
        text="",
        html="",
        current_url=SOURCE_URL,
        present_selectors=None,
        download_dir=None,
        fail_download_urls=None,
        fail_click_titles=None,
        download_contents=None,
        download_mime="application/octet-stream",
    ):
        self.text = text
        self.html = html
        self.current_url = current_url
        self.present_selectors = set(present_selectors or [])
        self.download_dir = download_dir
        self.fail_download_urls = set(fail_download_urls or [])
        self.fail_click_titles = set(fail_click_titles or [])
        self.download_contents = download_contents or {}
        self.download_mime = download_mime
        self.navigated: list[str] = []
        self.clicks: list[str] = []
        self.downloads: list[str] = []
        self.click_downloads: list[str] = []
        self.rendered_calls: list[dict] = []
        self.page_html_calls = 0
        self._n = 0

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
        self.page_html_calls += 1
        return self.html

    async def rendered_html(
        self, slug, *, wait_for_selector=None, wait_for_text=None, timeout_ms=15000
    ):
        self.rendered_calls.append(
            {"selector": wait_for_selector, "text": wait_for_text}
        )
        if wait_for_text is not None:
            satisfied = wait_for_text in (self.html or "")
        elif wait_for_selector is not None:
            satisfied = wait_for_selector in self.present_selectors
        else:
            satisfied = True
        return RenderedPage(
            html=self.html, wait_satisfied=satisfied, current_url=self.current_url
        )

    async def element_exists(self, slug, selector):
        return selector in self.present_selectors

    async def click(self, slug, selector):
        self.clicks.append(selector)
        return {"ok": True, "current_url": self.current_url}

    def _write(self, name: str, content: bytes, mime: str) -> BridgeFile:
        safe = name.replace("/", "_").replace("\\", "_")
        p = Path(self.download_dir) / safe
        p.write_bytes(content)
        return BridgeFile(path=safe, size_bytes=p.stat().st_size, mime_type=mime)

    async def download(self, slug, url, dest_filename=None):
        self.downloads.append(url)
        if url in self.fail_download_urls:
            raise BridgeError(f"download failed for {url}")
        self._n += 1
        content = self.download_contents.get(url, b"%PDF body " + str(self._n).encode())
        return self._write(f"dl_{self._n}.bin", content, self.download_mime)

    async def click_download(self, slug, selector, dest_filename=None):
        self.click_downloads.append(selector)
        if any(t in selector for t in self.fail_click_titles):
            raise BridgeError(f"click-download failed for {selector}")
        self._n += 1
        return self._write(f"click_{self._n}.bin", b"%PDF click " + str(self._n).encode(),
                           self.download_mime)

    async def close_session(self, slug):
        return {"closed": True}


def _ctx(bridge, *, tender_id=920100, source_url=SOURCE_URL, description=None,
         candidate_urls=None, tender_ref="REF-PC", title=None):
    return PortalContext(
        portal_id=1,
        user_id="tester",
        domain="procontract.due-north.com",
        bridge=bridge,
        platform_slug="procontract",
        tender_id=tender_id,
        candidate_urls=candidate_urls or [],
        source_url=source_url,
        description=description,
        tender_ref=tender_ref,
        title=title,
    )


def _patch_storage(monkeypatch, tmp_path):
    from tender_agent.services.portals.adapters import proactis as mod

    bridge_dl = tmp_path / "bridge-dl"
    bridge_dl.mkdir()
    storage = tmp_path / "storage"
    monkeypatch.setattr(mod.settings, "bridge_download_dir", str(bridge_dl))
    monkeypatch.setattr(mod.settings, "document_storage_dir", str(storage))
    return bridge_dl, storage


# --- rfxId extraction ---------------------------------------------------


def test_extract_rfx_id_from_url():
    assert extract_rfx_id(SOURCE_URL) == RFX


def test_extract_rfx_id_from_description():
    desc = f"Respond via https://procontract.due-north.com/RfxResponse?rfxId={RFX}&x=1"
    assert extract_rfx_id(None, desc) == RFX


def test_extract_rfx_id_none_when_absent():
    assert extract_rfx_id("https://example.com/x", "no id here", None) is None


# --- classification / login --------------------------------------------


def test_matches_url():
    a = ProactisAdapter()
    assert a.matches_url("https://procontract.due-north.com/RfxResponse?rfxId=1") is True
    assert a.matches_url("https://buyer.due-north.com/x") is True
    assert a.matches_url("https://www.delta-esourcing.com/z") is False


def test_requires_login_and_login_url():
    a = ProactisAdapter()
    assert a.requires_login is True
    assert "due-north.com" in a.login_url()


@pytest.mark.asyncio
async def test_is_authenticated_true_when_marker_present():
    bridge = FakeBridge(
        current_url="https://procontract.due-north.com/SupplierPostLoginHome",
        present_selectors={PROACTIS_SELECTORS["logged_in_marker"]},
    )
    assert await ProactisAdapter().is_authenticated(_ctx(bridge)) is True


@pytest.mark.asyncio
async def test_is_authenticated_false_on_login_redirect():
    bridge = FakeBridge(
        current_url="https://procontract.due-north.com/Login/Index",
        present_selectors={PROACTIS_SELECTORS["logged_in_marker"]},
    )
    assert await ProactisAdapter().is_authenticated(_ctx(bridge)) is False


@pytest.mark.asyncio
async def test_is_authenticated_false_without_marker():
    bridge = FakeBridge(
        current_url="https://procontract.due-north.com/SupplierPostLoginHome",
        present_selectors=set(),
    )
    assert await ProactisAdapter().is_authenticated(_ctx(bridge)) is False


@pytest.mark.asyncio
async def test_authenticate_is_human_login():
    res = await ProactisAdapter().authenticate(_ctx(FakeBridge()), Credentials())
    assert res.status == AuthStatus.success


# --- locate -------------------------------------------------------------


@pytest.mark.asyncio
async def test_locate_already_interested_via_rfx_id_found_no_pause():
    # Activity exists (documents present) → FOUND + already_authorized (no pause).
    bridge = FakeBridge(
        html=_knowsley_activity(),
        present_selectors={PROACTIS_SELECTORS["document_rows"]},
    )
    adapter = ProactisAdapter()
    res = await adapter.locate_tender(_ctx(bridge, title="DPS for Landscape"), "REF-PC")
    assert res.status == LocateStatus.found
    assert res.already_authorized is True
    assert res.detail == ALREADY_INTERESTED_DETAIL
    assert adapter._rfx_id == RFX
    assert any(f"rfxId={RFX}" in u for u in bridge.navigated)
    assert bridge.clicks == []  # never expressed interest again


@pytest.mark.asyncio
async def test_locate_found_via_activities_title_match():
    # No rfxId in the tender → title-match the My-activities listing.
    listing = _activities_listing(
        [
            ("aaa-1", "Grounds Maintenance Framework"),
            (RFX, "Dynamic Purchasing System for Landscape Contractors"),
            ("ccc-3", "IT Support Services"),
        ]
    )
    bridge = FakeBridge(html=listing)
    adapter = ProactisAdapter()
    res = await adapter.locate_tender(
        _ctx(bridge, source_url=None, tender_ref="REF",
             title="Dynamic Purchasing System for Landscape Contractors"),
        "REF",
    )
    assert res.status == LocateStatus.found
    assert res.already_authorized is True
    assert adapter._rfx_id == RFX
    assert adapter._activity_url == PROACTIS_URLS["activity"] % RFX


@pytest.mark.asyncio
async def test_locate_not_interested_gates_on_express_interest():
    # rfxId present but the activity isn't available and no email indicator, and
    # not in My-activities → Express Interest gate (the pause).
    bridge = FakeBridge(html="<html><body>Please express interest.</body></html>")
    adapter = ProactisAdapter()
    res = await adapter.locate_tender(_ctx(bridge, title="Some opportunity"), "REF-PC")
    assert res.status == LocateStatus.requires_interest_first
    assert res.already_authorized is False
    assert res.detail == EXPRESS_INTEREST_DETAIL
    assert bridge.clicks == []  # the click is user-gated


@pytest.mark.asyncio
async def test_locate_email_confirmation_pauses_with_awaiting_detail():
    # Expressed interest, buyer must confirm by email → a clean awaiting pause.
    bridge = FakeBridge(
        html=(
            "<html><body><div>Your expression of interest has been received. "
            "You will receive an email shortly.</div></body></html>"
        ),
    )
    adapter = ProactisAdapter()
    res = await adapter.locate_tender(_ctx(bridge, title="Pending opportunity"), "REF-PC")
    assert res.status == LocateStatus.requires_interest_first
    assert res.detail == AWAITING_BUYER_RELEASE_DETAIL
    assert res.already_authorized is False


@pytest.mark.asyncio
async def test_locate_not_found_without_id_or_match():
    bridge = FakeBridge(html=_activities_listing([("zzz", "Unrelated thing")]))
    adapter = ProactisAdapter()
    res = await adapter.locate_tender(
        _ctx(bridge, source_url=None, title="Totally different tender"), "REF"
    )
    assert res.status == LocateStatus.not_found


# --- register interest (only after user confirm) -----------------------


@pytest.mark.asyncio
async def test_register_interest_clicks_express_interest():
    bridge = FakeBridge(
        present_selectors={PROACTIS_SELECTORS["express_interest_button"]},
        text="Your expression of interest has been submitted successfully.",
    )
    adapter = ProactisAdapter()
    res = await adapter.register_interest(_ctx(bridge))
    assert res.status == RegisterStatus.success
    assert PROACTIS_SELECTORS["express_interest_button"] in bridge.clicks


@pytest.mark.asyncio
async def test_register_interest_skips_when_already_expressed():
    # No Express Interest control + an already-expressed marker → already_registered,
    # never click.
    bridge = FakeBridge(
        present_selectors=set(),
        text="You have already expressed interest in this opportunity.",
    )
    adapter = ProactisAdapter()
    res = await adapter.register_interest(_ctx(bridge))
    assert res.status == RegisterStatus.already_registered
    assert bridge.clicks == []


# --- download: BOTH sections, direct-link + click fallback -------------


@pytest.mark.asyncio
async def test_download_reads_both_sections(tmp_path, monkeypatch):
    bridge_dl, storage = _patch_storage(monkeypatch, tmp_path)
    bridge = FakeBridge(
        html=_knowsley_activity(),
        current_url=ACTIVITY_URL,
        download_dir=str(bridge_dl),
    )
    adapter = ProactisAdapter()
    adapter._rfx_id = RFX
    res = await adapter.download_documents(_ctx(bridge), "ignored")
    assert res.status == DownloadStatus.complete
    # 3 documentation files + 1 terms file = 4 captured from BOTH sections.
    assert len(res.files) == 4
    names = {f.filename for f in res.files}
    assert "Section_1_Exec_Summary_and_Specification_v6.docx" in names
    assert "Services__without_TUPE_" in {n.rsplit(".", 1)[0] for n in names} or any(
        n.startswith("Services") for n in names
    )
    # Both document areas were downloaded via the direct-link (cookie GET) path.
    assert len(bridge.downloads) == 4
    assert bridge.click_downloads == []
    for f in res.files:
        assert f.sha256 and f.storage_key
        assert (storage / f.storage_key).is_file()
    # Distinct, non-null source urls (no unique-constraint collapse).
    urls = [f.url for f in res.files]
    assert len(set(urls)) == 4 and all(urls)


@pytest.mark.asyncio
async def test_download_direct_link_and_click_fallback_both_covered(tmp_path, monkeypatch):
    bridge_dl, _ = _patch_storage(monkeypatch, tmp_path)
    # Two docs: one downloads directly; one whose direct GET fails → click
    # fallback; one with a non-document href (#) → click fallback.
    docs = [
        ("Spec.pdf", "/Download/Attachment/ok"),
        ("Broken.pdf", "/Download/Attachment/broken"),
    ]
    terms = [("Scripted Terms", "#")]
    html = _activity_html(docs, terms)
    broken_url = "https://procontract.due-north.com/Download/Attachment/broken"
    bridge = FakeBridge(
        html=html,
        current_url=ACTIVITY_URL,
        download_dir=str(bridge_dl),
        fail_download_urls={broken_url},
    )
    adapter = ProactisAdapter()
    adapter._rfx_id = RFX
    res = await adapter.download_documents(_ctx(bridge), "ignored")
    assert res.status == DownloadStatus.complete
    assert len(res.files) == 3
    # Direct-link tried for the two real hrefs; click fallback covered the failed
    # GET and the scripted (#) link.
    assert "https://procontract.due-north.com/Download/Attachment/ok" in bridge.downloads
    assert broken_url in bridge.downloads
    assert len(bridge.click_downloads) == 2  # Broken.pdf + Scripted Terms


@pytest.mark.asyncio
async def test_download_dedups_identical_content(tmp_path, monkeypatch):
    bridge_dl, _ = _patch_storage(monkeypatch, tmp_path)
    docs = [("A.pdf", "/d/a"), ("B.pdf", "/d/b")]
    terms = [("C terms", "/d/c")]
    html = _activity_html(docs, terms)
    same = b"%PDF identical bytes"
    contents = {
        "https://procontract.due-north.com/d/a": same,
        "https://procontract.due-north.com/d/b": same,
        "https://procontract.due-north.com/d/c": same,
    }
    bridge = FakeBridge(
        html=html, current_url=ACTIVITY_URL, download_dir=str(bridge_dl),
        download_contents=contents,
    )
    adapter = ProactisAdapter()
    adapter._rfx_id = RFX
    res = await adapter.download_documents(_ctx(bridge), "ignored")
    assert len(res.files) == 1  # three identical -> one kept after sha256 dedup


@pytest.mark.asyncio
async def test_download_caps_at_max_docs(tmp_path, monkeypatch):
    from tender_agent.services.portals.adapters import proactis as mod

    bridge_dl, _ = _patch_storage(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "MAX_DOCS", 2)
    docs = [("A.pdf", "/d/a"), ("B.pdf", "/d/b"), ("C.pdf", "/d/c")]
    bridge = FakeBridge(
        html=_activity_html(docs, []), current_url=ACTIVITY_URL,
        download_dir=str(bridge_dl),
    )
    adapter = ProactisAdapter()
    adapter._rfx_id = RFX
    res = await adapter.download_documents(_ctx(bridge), "ignored")
    assert len(res.files) == 2
    assert len(res.missing) == 1
    assert res.status == DownloadStatus.partial


@pytest.mark.asyncio
async def test_download_partial_when_some_fail(tmp_path, monkeypatch):
    bridge_dl, _ = _patch_storage(monkeypatch, tmp_path)
    docs = [("A.pdf", "/d/a"), ("B.pdf", "/d/b")]
    bridge = FakeBridge(
        html=_activity_html(docs, []), current_url=ACTIVITY_URL,
        download_dir=str(bridge_dl),
        fail_download_urls={"https://procontract.due-north.com/d/b"},
        fail_click_titles={"B.pdf"},  # click fallback also fails
    )
    adapter = ProactisAdapter()
    adapter._rfx_id = RFX
    res = await adapter.download_documents(_ctx(bridge), "ignored")
    assert res.status == DownloadStatus.partial
    assert len(res.files) == 1
    assert res.missing == ["B.pdf"]


@pytest.mark.asyncio
async def test_download_error_when_all_fail(tmp_path, monkeypatch):
    bridge_dl, _ = _patch_storage(monkeypatch, tmp_path)
    docs = [("A.pdf", "/d/a"), ("B.pdf", "/d/b")]
    bridge = FakeBridge(
        html=_activity_html(docs, []), current_url=ACTIVITY_URL,
        download_dir=str(bridge_dl),
        fail_download_urls={
            "https://procontract.due-north.com/d/a",
            "https://procontract.due-north.com/d/b",
        },
        fail_click_titles={"A.pdf", "B.pdf"},
    )
    adapter = ProactisAdapter()
    adapter._rfx_id = RFX
    res = await adapter.download_documents(_ctx(bridge), "ignored")
    assert res.status == DownloadStatus.error
    assert res.status != DownloadStatus.nothing_available
    assert res.files == []
    assert "could not download any" in res.detail


@pytest.mark.asyncio
async def test_download_email_confirmation_awaiting_state(tmp_path, monkeypatch):
    # The activity shows the email-confirmation notice and NO documents → a clean
    # awaiting-buyer-release state (nothing_available, not an error).
    bridge_dl, _ = _patch_storage(monkeypatch, tmp_path)
    bridge = FakeBridge(
        html=(
            "<html><body><div>Your expression of interest has been received. "
            "You will receive an email shortly.</div></body></html>"
        ),
        current_url=ACTIVITY_URL,
        download_dir=str(bridge_dl),
    )
    adapter = ProactisAdapter()
    adapter._rfx_id = RFX
    res = await adapter.download_documents(_ctx(bridge), "ignored")
    assert res.status == DownloadStatus.nothing_available
    assert res.status != DownloadStatus.error
    assert res.detail == AWAITING_BUYER_RELEASE_DETAIL


@pytest.mark.asyncio
async def test_download_errors_without_rfx_id(tmp_path, monkeypatch):
    bridge_dl, _ = _patch_storage(monkeypatch, tmp_path)
    bridge = FakeBridge(html="", current_url="https://procontract.due-north.com/x",
                        download_dir=str(bridge_dl))
    adapter = ProactisAdapter()
    res = await adapter.download_documents(
        _ctx(bridge, source_url="https://procontract.due-north.com/x"), "ignored"
    )
    assert res.status == DownloadStatus.error
    assert "rfxId" in res.detail


@pytest.mark.asyncio
async def test_download_reads_rendered_dom_not_page_html(tmp_path, monkeypatch):
    bridge_dl, _ = _patch_storage(monkeypatch, tmp_path)
    bridge = FakeBridge(
        html=_knowsley_activity(), current_url=ACTIVITY_URL,
        download_dir=str(bridge_dl),
        present_selectors={PROACTIS_SELECTORS["document_rows"]},
    )
    adapter = ProactisAdapter()
    adapter._rfx_id = RFX
    res = await adapter.download_documents(_ctx(bridge), "ignored")
    assert res.status == DownloadStatus.complete
    assert bridge.rendered_calls
    assert bridge.page_html_calls == 0


# --- email-confirmation RESUME: activity appears later → fetches --------


@pytest.mark.asyncio
async def test_email_case_resume_fetches_when_activity_appears(tmp_path, monkeypatch):
    """The EMAIL-CONFIRMATION resume: once the buyer confirms and the activity is
    live, a fresh locate finds it (FOUND) and download captures the documents."""
    bridge_dl, storage = _patch_storage(monkeypatch, tmp_path)
    bridge = FakeBridge(
        html=_knowsley_activity(),
        current_url=ACTIVITY_URL,
        download_dir=str(bridge_dl),
        present_selectors={PROACTIS_SELECTORS["document_rows"]},
    )
    adapter = ProactisAdapter()
    located = await adapter.locate_tender(_ctx(bridge, title="DPS Landscape"), "REF-PC")
    assert located.status == LocateStatus.found  # activity now live
    res = await adapter.download_documents(_ctx(bridge), "ignored")
    assert res.status == DownloadStatus.complete
    assert len(res.files) == 4


# --- SHARED persistence + content extraction (DB-backed) ----------------


@pytest.fixture()
def db() -> Session:
    connection = engine.connect()
    outer = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        connection.close()


def _seed_tender(db: Session, ref: str) -> Tender:
    now = datetime.now(UTC)
    t = Tender(source_code="FTS", source_ref=ref, title=f"Proactis {ref}",
               source_url=SOURCE_URL, first_seen_at=now, last_seen_at=now)
    db.add(t)
    db.flush()
    return t


@pytest.mark.asyncio
async def test_files_flow_through_shared_persistence_and_content(
    db: Session, tmp_path, monkeypatch
):
    """The captured files must persist as tender_document_files AND extract into
    tender_document_content via the SHARED orchestrator path — not a Proactis
    path. Uses .txt content so the extractor yields real text."""
    bridge_dl, storage = _patch_storage(monkeypatch, tmp_path)
    docs = [("Spec.txt", "/d/spec"), ("Pricing.txt", "/d/price")]
    terms = [("Terms.txt", "/d/terms")]
    contents = {
        "https://procontract.due-north.com/d/spec": b"ITT specification content here.",
        "https://procontract.due-north.com/d/price": b"Pricing schedule content.",
        "https://procontract.due-north.com/d/terms": b"Terms and conditions content.",
    }
    bridge = FakeBridge(
        html=_activity_html(docs, terms), current_url=ACTIVITY_URL,
        download_dir=str(bridge_dl), download_contents=contents,
        download_mime="text/plain",
    )
    adapter = ProactisAdapter()
    adapter._rfx_id = RFX
    download = await adapter.download_documents(_ctx(bridge), "ignored")
    assert download.status == DownloadStatus.complete
    assert len(download.files) == 3

    t = _seed_tender(db, "pc-persist-1")
    persisted = PortalOrchestrator()._persist_documents(db, t, download)
    assert persisted == 3

    files = db.execute(
        select(TenderDocumentFile).where(TenderDocumentFile.tender_id == t.id)
    ).scalars().all()
    assert len(files) == 3  # all recorded, not collapsed
    assert {f.title for f in files} == {"Spec.txt", "Pricing.txt", "Terms.txt"}
    assert all(f.download_status == "ok" and f.storage_key for f in files)

    # Content extracted into tender_document_content via ensure_content_extracted.
    content_rows = db.execute(
        select(TenderDocumentContent).where(TenderDocumentContent.tender_id == t.id)
    ).scalars().all()
    assert len(content_rows) == 3
    assert all(c.extraction_status == "ok" for c in content_rows)
    assert any("specification" in (c.extracted_text or "").lower() for c in content_rows)

    # Re-persist is idempotent (sha256 dedup): still three rows.
    again = PortalOrchestrator()._persist_documents(db, t, download)
    assert again == 3
    files2 = db.execute(
        select(TenderDocumentFile).where(TenderDocumentFile.tender_id == t.id)
    ).scalars().all()
    assert len(files2) == 3
