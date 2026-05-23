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

from tender_agent.services.bridge_client import BridgeFile, RenderedPage
from tender_agent.services.portals.adapters.delta_esourcing import (
    ALREADY_REGISTERED_DETAIL,
    CONCURRENT_LOGIN_DETAIL,
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
CONCURRENT_BANNER = "Concurrent Logins Are Not Enabled — currently in use."
STAGE_ONE_URL = (
    "https://www.delta-esourcing.com/delta/suppliers/select/"
    "suppRespStatus.html?id=555&listId=777"
)


def _responses_table_html(rows):
    """Build a Response Manager "Responses" table. Each row is
    (resp_id, list_id, opportunity_title); the title is a link whose href
    carries both Stage One ids (the real already-registered mechanism)."""
    trs = []
    for resp_id, list_id, title in rows:
        href = (
            "https://www.delta-esourcing.com/delta/suppliers/select/"
            f"suppRespStatus.html?id={resp_id}&amp;listId={list_id}"
        )
        trs.append(
            f"<tr><td><a href='{href}'>{title}</a></td><td>ITT</td>"
            "<td>No</td><td>01/05/2026</td><td>In Progress</td>"
            "<td>12/06/2026</td><td>Buyer</td></tr>"
        )
    return (
        "<table class='responses'><tr><th>Opportunity</th>"
        "<th>Opportunity Type</th><th>Submitted</th><th>Submitted Date</th>"
        "<th>Status</th><th>Closing Date</th><th>Owner</th></tr>"
        + "".join(trs) + "</table>"
    )


def _docs_table_html(resp_id="555", list_id="777", n=3, titles=None, items=None):
    """Build a Stage One documents table with n rows, each carrying a direct
    downloadDocument link (the real download mechanism). When `items` is given,
    append Delta's "Displaying 1 - n of <items> items" footer so partial-render
    detection (rows-found vs items-displayed) can be exercised."""
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
    footer = f"<div>Displaying 1 - {n} of {items} items</div>" if items else ""
    return (
        "<table><tr><th>Document Title</th><th>Size</th><th>File Type</th>"
        "<th>Uploaded</th><th>Action</th></tr>" + "".join(rows) + "</table>" + footer
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
        self.rendered_calls: list[dict] = []
        self.page_html_calls = 0

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
        # The adapter no longer reads the JS-rendered tables via page_html;
        # tracked so tests can assert the rendered-DOM path is used instead.
        self.page_html_calls += 1
        return self.html

    def _selector_satisfied(self, selector):
        """Approximate Playwright wait_for_selector against the canned html, for
        the Delta selectors the adapter waits on. Lets selector-based renders
        (chunk 4h) be exercised without a real browser."""
        s = selector or ""
        html = self.html or ""
        if "suppRespStatus.html" in s:  # responses_opportunity_link
            return "suppRespStatus.html" in html
        if "responses" in s or "Opportunity" in s:  # responses_table container
            return ("Opportunity" in html) or ("suppRespStatus.html" in html)
        if any(
            m in s
            for m in ("Response Manager", "Profile Manager", "supplierMenu",
                      "Select Accredit", "Resources")
        ):  # logged_in_marker
            return any(
                m in html
                for m in ("Response Manager", "Profile Manager",
                          "Supplier Administrator", "supplierMenu")
            )
        if "downloadDocument" in s:
            return "downloadDocument" in html
        return False

    async def rendered_html(
        self, slug, *, wait_for_selector=None, wait_for_text=None, timeout_ms=15000
    ):
        """Return self.html as the "rendered DOM". wait_satisfied mirrors whether
        the requested marker is present — wait_for_text in the html, or
        wait_for_selector matching present_selectors / a heuristic against the
        html — so the adapter's wait logic is exercised as against the live
        bridge."""
        self.rendered_calls.append(
            {"selector": wait_for_selector, "text": wait_for_text,
             "timeout_ms": timeout_ms}
        )
        if wait_for_text is not None:
            satisfied = wait_for_text in (self.html or "")
        elif wait_for_selector is not None:
            satisfied = (
                wait_for_selector in self.present_selectors
                or self._selector_satisfied(wait_for_selector)
            )
        else:
            satisfied = True
        return RenderedPage(
            html=self.html, wait_satisfied=satisfied, current_url=self.current_url
        )

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
         description=None, tender_ref="REF-123", title=None):
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
        title=title,
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
async def test_locate_already_registered_reads_ids_from_responses_table():
    # The tender is already in the Responses table; its opportunity link carries
    # the Stage One ids (note &amp; encoding, like real Delta markup).
    rm_html = _responses_table_html(
        [("1038167190", "1036629453", "Provision of Cleaning Services")]
    )
    bridge = FakeBridge(html=rm_html, text="My Responses")
    adapter = DeltaEsourcingAdapter()
    res = await adapter.locate_tender(
        _ctx(
            bridge,
            source_url="https://www.delta-esourcing.com/respond/286EVX23TV",
            title="Provision of Cleaning Services for Council",
        ),
        "REF-123",
    )
    # Still pauses for confirmation before the FIRST fetch, but with the ids
    # captured straight from the link and NO Register Interest click.
    assert res.status == LocateStatus.requires_interest_first
    assert res.detail == ALREADY_REGISTERED_DETAIL
    assert (adapter._resp_id, adapter._list_id) == ("1038167190", "1036629453")
    assert bridge.clicks == []
    # Only the Response Manager was visited — no notice page, no re-registration.
    assert any("addToList.html" in u for u in bridge.navigated)
    assert not any("respondToList.html" in u for u in bridge.navigated)


@pytest.mark.asyncio
async def test_locate_already_registered_reads_rendered_dom_not_page_html():
    # The Responses table is a JS-rendered bip-table too — locate must read it
    # via rendered_html (waiting for the opportunity links), not the raw shell.
    rm_html = _responses_table_html(
        [("1038167190", "1036629453", "Provision of Cleaning Services")]
    )
    bridge = FakeBridge(html=rm_html, text="My Responses")
    adapter = DeltaEsourcingAdapter()
    res = await adapter.locate_tender(
        _ctx(
            bridge,
            source_url="https://www.delta-esourcing.com/respond/286EVX23TV",
            title="Provision of Cleaning Services for Council",
        ),
        "REF-123",
    )
    assert res.status == LocateStatus.requires_interest_first
    assert (adapter._resp_id, adapter._list_id) == ("1038167190", "1036629453")
    # Read the rendered DOM, WAITING ON THE ACTUAL OPPORTUNITY-LINK ELEMENT
    # (chunk 4h: a real element wait, not a text-substring poll); never the raw
    # page-html shell.
    assert any(
        c["selector"] == DELTA_SELECTORS["responses_opportunity_link"]
        for c in bridge.rendered_calls
    )
    assert bridge.page_html_calls == 0


@pytest.mark.asyncio
async def test_locate_already_registered_picks_best_title_match():
    # Several registered opportunities — match THIS tender by title.
    rm_html = _responses_table_html([
        ("111", "222", "Grounds Maintenance Framework"),
        ("1038167190", "1036629453", "Provision of Cleaning Services"),
        ("333", "444", "IT Support Services"),
    ])
    bridge = FakeBridge(html=rm_html, text="My Responses")
    adapter = DeltaEsourcingAdapter()
    res = await adapter.locate_tender(
        _ctx(
            bridge,
            source_url="https://www.delta-esourcing.com/respond/286EVX23TV",
            title="Provision of Cleaning Services",
        ),
        "REF-123",
    )
    assert res.status == LocateStatus.requires_interest_first
    assert (adapter._resp_id, adapter._list_id) == ("1038167190", "1036629453")
    assert bridge.clicks == []


@pytest.mark.asyncio
async def test_locate_not_registered_gates_on_register_after_responses_check():
    # Empty Responses table → not registered → notice page → REGISTER INTEREST.
    bridge = FakeBridge(
        html=_responses_table_html([]),
        text="This tender is currently OPEN. REGISTER INTEREST to bid.",
    )
    adapter = DeltaEsourcingAdapter()
    res = await adapter.locate_tender(
        _ctx(
            bridge,
            source_url="https://www.delta-esourcing.com/respond/286EVX23TV",
            title="Some Other Tender",
        ),
        "REF-123",
    )
    assert res.status == LocateStatus.requires_interest_first
    assert res.detail != ALREADY_REGISTERED_DETAIL
    assert (adapter._resp_id, adapter._list_id) == (None, None)
    # Checked the Responses table FIRST, then opened the notice page.
    assert any("addToList.html" in u for u in bridge.navigated)
    assert any(
        "respondToList.html?accessCode=286EVX23TV" in u for u in bridge.navigated
    )
    assert bridge.clicks == []


# --- chunk 4h: robust Responses-table read (the render race) -----------


class RaceBridge(FakeBridge):
    """Simulates the bip-table render race: the opportunity-link rows only
    "render" (become wait-satisfied) on/after the Nth Response-Manager
    navigation. Before then the page is an empty shell — exactly the timing that
    made already-registered detection intermittent."""

    def __init__(self, *, ready_html, ready_after_nav=2, shell_html=None, **kw):
        super().__init__(html=shell_html or "<html><body>loading…</body></html>", **kw)
        self._ready_html = ready_html
        self._shell_html = shell_html or "<html><body>loading…</body></html>"
        self._ready_after_nav = ready_after_nav
        self._rm_navs = 0

    async def navigate(self, slug, url):
        self.navigated.append(url)
        if "addToList.html" in url:  # the Response Manager
            self._rm_navs += 1
            self.html = (
                self._ready_html
                if self._rm_navs >= self._ready_after_nav
                else self._shell_html
            )
        return {"current_url": self.current_url}


def _rm_nav_count(bridge):
    return len([u for u in bridge.navigated if "addToList.html" in u])


@pytest.mark.asyncio
async def test_responses_race_detects_already_registered():
    # Rows absent on the first read, present after a re-navigate (retry) → the
    # adapter must DETECT already-registered, not fall through to register.
    rm_html = _responses_table_html(
        [("1038167190", "1036629453", "Provision of Cleaning Services")]
    )
    bridge = RaceBridge(ready_html=rm_html, ready_after_nav=2, text="My Responses")
    adapter = DeltaEsourcingAdapter()
    res = await adapter.locate_tender(
        _ctx(
            bridge,
            source_url="https://www.delta-esourcing.com/respond/286EVX23TV",
            title="Provision of Cleaning Services for Council",
        ),
        "REF-123",
    )
    assert res.status == LocateStatus.requires_interest_first
    assert res.detail == ALREADY_REGISTERED_DETAIL
    assert res.already_authorized is True
    assert (adapter._resp_id, adapter._list_id) == ("1038167190", "1036629453")
    # It retried (re-navigated to the Response Manager) before concluding.
    assert _rm_nav_count(bridge) >= 2
    # Never opened the notice page / register path.
    assert not any("respondToList.html" in u for u in bridge.navigated)


@pytest.mark.asyncio
async def test_responses_genuinely_empty_no_retry():
    # A positive "no responses" indicator is trusted as genuinely-empty: not
    # registered, and NO retry loop (one Response-Manager read only).
    empty_html = "<html><body><div>You have no responses yet.</div></body></html>"
    bridge = FakeBridge(html=empty_html, text="My Responses")
    adapter = DeltaEsourcingAdapter()
    res = await adapter.locate_tender(
        _ctx(
            bridge,
            source_url="https://www.delta-esourcing.com/respond/286EVX23TV",
            title="Some Tender",
        ),
        "REF-123",
    )
    # Not already-registered → falls through to the notice/register path.
    assert res.detail != ALREADY_REGISTERED_DETAIL
    assert res.already_authorized is False
    assert (adapter._resp_id, adapter._list_id) == (None, None)
    # Positive empty → no retry: the Response Manager was read exactly once.
    assert _rm_nav_count(bridge) == 1


@pytest.mark.asyncio
async def test_responses_retry_then_found_logs_outcome():
    # First read times out unrendered, the retry finds the row → already-registered.
    rm_html = _responses_table_html([("999", "888", "Cleaning Services")])
    bridge = RaceBridge(ready_html=rm_html, ready_after_nav=2, text="My Responses")
    adapter = DeltaEsourcingAdapter()
    res = await adapter.locate_tender(
        _ctx(
            bridge,
            source_url="https://www.delta-esourcing.com/respond/286EVX23TV",
            title="Cleaning Services",
        ),
        "REF-123",
    )
    assert res.status == LocateStatus.requires_interest_first
    assert res.already_authorized is True
    assert (adapter._resp_id, adapter._list_id) == ("999", "888")


@pytest.mark.asyncio
async def test_responses_unrendered_both_times_falls_through():
    # The table never renders (both reads unrendered) → not-registered path,
    # bounded (does not loop forever).
    bridge = RaceBridge(
        ready_html=_responses_table_html([("1", "2", "X")]),
        ready_after_nav=99,  # never becomes ready
        text="This tender is currently OPEN. REGISTER INTEREST to bid.",
    )
    adapter = DeltaEsourcingAdapter()
    res = await adapter.locate_tender(
        _ctx(
            bridge,
            source_url="https://www.delta-esourcing.com/respond/286EVX23TV",
            title="Some Tender",
        ),
        "REF-123",
    )
    assert res.status == LocateStatus.requires_interest_first
    assert res.already_authorized is False  # the register path, not already-reg
    # Exactly one retry (two Response-Manager reads), then it gave up and opened
    # the notice page — bounded, not an infinite loop.
    assert _rm_nav_count(bridge) == 2
    assert any("respondToList.html" in u for u in bridge.navigated)


@pytest.mark.asyncio
async def test_is_authenticated_true_via_rendered_marker():
    # An already-authenticated session whose supplier menu is in the RENDERED DOM
    # is reliably detected (chunk 4h) — even without present_selectors.
    bridge = FakeBridge(
        html="<nav><a href='#'>Response Manager</a><a href='#'>Profile Manager</a></nav>",
        current_url="https://www.delta-esourcing.com/delta/suppliers/select/addToList.html",
    )
    assert await DeltaEsourcingAdapter().is_authenticated(_ctx(bridge)) is True


@pytest.mark.asyncio
async def test_locate_concurrent_login_returns_clear_error():
    bridge = FakeBridge(text=CONCURRENT_BANNER)
    adapter = DeltaEsourcingAdapter()
    res = await adapter.locate_tender(
        _ctx(bridge, source_url="https://www.delta-esourcing.com/respond/286EVX23TV"),
        "REF-123",
    )
    assert res.status == LocateStatus.error
    assert res.detail == CONCURRENT_LOGIN_DETAIL
    # Bailed at the Response Manager — never opened the notice page.
    assert not any("respondToList.html" in u for u in bridge.navigated)


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


@pytest.mark.asyncio
async def test_register_interest_skips_click_when_already_registered():
    # locate already captured the ids → never re-register (never click).
    bridge = FakeBridge(current_url=STAGE_ONE_URL)
    adapter = DeltaEsourcingAdapter()
    adapter._resp_id, adapter._list_id = "555", "777"
    res = await adapter.register_interest(_ctx(bridge))
    assert res.status == RegisterStatus.already_registered
    assert bridge.clicks == []
    assert bridge.navigated == []


@pytest.mark.asyncio
async def test_register_interest_errors_when_no_stage_one_after_click():
    # Click happens but Delta neither redirects to Stage One nor lists the row.
    bridge = FakeBridge(
        current_url="https://www.delta-esourcing.com/delta/respondToList.html?x=1",
        html=_responses_table_html([]),
    )
    adapter = DeltaEsourcingAdapter()
    res = await adapter.register_interest(
        _ctx(bridge, source_url="https://www.delta-esourcing.com/respond/286EVX23TV")
    )
    assert res.status == RegisterStatus.error
    assert "could not locate Stage One" in res.detail
    assert DELTA_SELECTORS["register_interest_button"] in bridge.clicks


# --- session conflict + logout (single-session constraint) -------------


@pytest.mark.asyncio
async def test_session_conflict_detects_concurrent_login():
    bridge = FakeBridge(text=CONCURRENT_BANNER)
    msg = await DeltaEsourcingAdapter().session_conflict(_ctx(bridge))
    assert msg == CONCURRENT_LOGIN_DETAIL
    assert any("addToList.html" in u for u in bridge.navigated)


@pytest.mark.asyncio
async def test_session_conflict_none_when_no_conflict():
    bridge = FakeBridge(text="My Responses — all good")
    assert await DeltaEsourcingAdapter().session_conflict(_ctx(bridge)) is None


@pytest.mark.asyncio
async def test_logout_navigates_to_logout_url():
    bridge = FakeBridge()
    ok = await DeltaEsourcingAdapter().logout(_ctx(bridge))
    assert ok is True
    assert DELTA_URLS["logout"] in bridge.navigated


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
    # Not a silent empty result — a clear detail naming the likely cause.
    assert "did not render" in res.detail


@pytest.mark.asyncio
async def test_download_reads_rendered_dom_not_page_html(tmp_path, monkeypatch):
    # The download links live only in the RENDERED DOM (bip-table JS render), so
    # the adapter must read via rendered_html — never the raw page-html shell.
    bridge_dl, storage = _patch_storage(monkeypatch, tmp_path)
    bridge = FakeBridge(
        html=_docs_table_html(n=3),
        current_url=STAGE_ONE_URL,
        download_dir=str(bridge_dl),
    )
    res = await DeltaEsourcingAdapter().download_documents(_ctx(bridge), "ignored")
    assert res.status == DownloadStatus.complete
    assert len(res.files) == 3
    # Rendered DOM read with a wait for the download-link marker; page_html unused.
    assert bridge.rendered_calls
    assert bridge.rendered_calls[0]["text"] == "downloadDocument"
    assert bridge.page_html_calls == 0


@pytest.mark.asyncio
async def test_download_builds_urls_from_known_ids_and_docid(tmp_path, monkeypatch):
    # Deterministic URL build: respId/listId come from the captured Stage One ids
    # and only docId varies per row — independent of the rendered href.
    bridge_dl, _ = _patch_storage(monkeypatch, tmp_path)
    bridge = FakeBridge(
        html=_docs_table_html(resp_id="555", list_id="777", n=3),
        current_url=STAGE_ONE_URL,
        download_dir=str(bridge_dl),
    )
    res = await DeltaEsourcingAdapter().download_documents(_ctx(bridge), "ignored")
    assert {f.url for f in res.files} == {
        DELTA_URLS["document_download"] % ("555", "777", str(1000 + i))
        for i in range(3)
    }


@pytest.mark.asyncio
async def test_download_nothing_available_on_empty_bip_table_shell(
    tmp_path, monkeypatch
):
    # Even after the render wait, the table is just the empty bip-table shell
    # (no download links) → nothing_available WITH the clear detail, not silent.
    bridge_dl, _ = _patch_storage(monkeypatch, tmp_path)
    shell = (
        "<bip-table s:id='docs'><bip-table-search></bip-table-search>"
        "<table><tr><th>Document Title</th><th>Size</th><th>File Type</th>"
        "<th>Action</th></tr></table></bip-table>"
    )
    bridge = FakeBridge(
        html=shell, current_url=STAGE_ONE_URL, download_dir=str(bridge_dl)
    )
    res = await DeltaEsourcingAdapter().download_documents(_ctx(bridge), "ignored")
    assert res.status == DownloadStatus.nothing_available
    assert "did not render" in res.detail
    # It DID wait for the render signal (and the fallbacks) before giving up.
    assert any(c["text"] == "downloadDocument" for c in bridge.rendered_calls)


@pytest.mark.asyncio
async def test_download_partial_render_downloads_found_rows(tmp_path, monkeypatch):
    # Only 10 of 22 rows rendered (Delta still shows "of 22 items"): download the
    # 10 found, mark partial, and do NOT return nothing_available.
    bridge_dl, _ = _patch_storage(monkeypatch, tmp_path)
    bridge = FakeBridge(
        html=_docs_table_html(n=10, items=22),
        current_url=STAGE_ONE_URL,
        download_dir=str(bridge_dl),
    )
    res = await DeltaEsourcingAdapter().download_documents(_ctx(bridge), "ignored")
    assert res.status == DownloadStatus.partial
    assert len(res.files) == 10
    assert res.status != DownloadStatus.nothing_available
    assert res.detail and "10 of 22" in res.detail


@pytest.mark.asyncio
async def test_download_full_render_matches_items_count_is_complete(
    tmp_path, monkeypatch
):
    # All 22 rendered and "of 22 items" → complete, no partial flag.
    bridge_dl, _ = _patch_storage(monkeypatch, tmp_path)
    bridge = FakeBridge(
        html=_docs_table_html(n=22, items=22),
        current_url=STAGE_ONE_URL,
        download_dir=str(bridge_dl),
    )
    res = await DeltaEsourcingAdapter().download_documents(_ctx(bridge), "ignored")
    assert res.status == DownloadStatus.complete
    assert len(res.files) == 22


def test_stage_one_and_download_urls_are_well_formed():
    assert DELTA_URLS["stage_one"] % ("555", "777") == STAGE_ONE_URL
    built = DELTA_URLS["document_download"] % ("555", "777", "1000")
    assert "respId=555" in built and "supplierListId=777" in built and "docId=1000" in built
