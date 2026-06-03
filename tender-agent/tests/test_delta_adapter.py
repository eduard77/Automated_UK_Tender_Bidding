"""Delta eSourcing adapter against a fully faked bridge. No browser, no network.

These exercise the REAL Stage-One flow: the access code rides in the URL (no
form-fill), documents are gated behind a REGISTER INTEREST button (pause), and
each document is downloaded by CLICKING its row's ⋮ Action menu → "Download
File" (chunk 4i — Delta exposes no per-row download link in the DOM, so the file
only downloads on the menu click). The Delta-specific selectors are the constants
the user validates manually.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tender_agent.services.bridge_client import BridgeError, BridgeRowDownload, RenderedPage
from tender_agent.services.portals.adapters.delta_esourcing import (
    ALREADY_REGISTERED_DETAIL,
    CONCURRENT_LOGIN_DETAIL,
    DELTA_LOGGED_IN_MARKERS,
    DELTA_LOGIN_SUCCESS_PATTERN,
    DELTA_SELECTORS,
    DELTA_URLS,
    DeltaEsourcingAdapter,
    _split_or_selectors,
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


def _docs_table_html(n=3, titles=None, file_types=None, items=None):
    """Build a Stage One documents table mirroring the live DOM (tender 3169):
    <table id="document" class="dataTable" role="grid"> inside div#documentList,
    a thead header row, and data rows as tr[role="row"] (alternating odd/even).
    Each row's Action cell holds the ⋮ icon (i.bip-actions-menu-link.fa-ellipsis-v
    inside <bip-actions-menu>) — NO download link (the file only downloads via the
    menu click). When `items` is given, append Delta's "Displaying 1 - n of
    <items> items" footer so partial-render detection can be exercised."""
    rows = []
    for i in range(n):
        title = titles[i] if titles else f"Document {i}.pdf"
        ft = file_types[i] if file_types else "PDF"
        parity = "odd" if i % 2 == 0 else "even"
        rows.append(
            f"<tr class='{parity}' role='row'>"
            f"<td>{title}</td><td>1 MB</td><td>{ft}</td><td>01/05/2026</td>"
            "<td><bip-actions-menu><div s:id='actionsMenu' class='bip-actions-menu'>"
            "<i s:id='actions_i' class='bip-actions-menu-link fa fa-ellipsis-v' "
            "tabindex='-1'></i></div></bip-actions-menu></td></tr>"
        )
    footer = (
        f"<div class='dataTables_info'>Displaying 1 - {n} of {items} items</div>"
        if items else ""
    )
    return (
        "<div id='documentList'>"
        "<table id='document' class='dataTable no-footer' role='grid'>"
        "<thead><tr role='row'><th>Document Title</th><th>Size</th>"
        "<th>File Type</th><th>Uploaded</th><th>Action</th></tr></thead><tbody>"
        + "".join(rows) + "</tbody></table></div>" + footer
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
        fail_rows=None,
        row_filenames=None,
        row_contents=None,
        row_mime="application/pdf",
        html_sequence=None,
    ):
        self.text = text
        self.html = html
        # Successive rendered_html() reads return successive html_sequence entries
        # (sticking on the last) — used to exercise the post-maximise poll where
        # the DataTables grid re-renders with more rows on a later read.
        self.html_sequence = list(html_sequence) if html_sequence else None
        self._html_idx = 0
        self.links = links or []
        self.current_url = current_url
        self.present_selectors = set(present_selectors or [])
        self.download_dir = download_dir
        # Per-row download behaviour for click_download_in_row:
        self.fail_rows = set(fail_rows or [])
        self.row_filenames = row_filenames or {}
        self.row_contents = row_contents or {}
        self.row_mime = row_mime
        self.navigated: list[str] = []
        self.fills: list[tuple[str, str]] = []
        self.clicks: list[str] = []
        self.selects: list[tuple[str, dict]] = []
        self.click_downloads: list[str] = []
        self.row_downloads: list[dict] = []
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

    def _current_html(self):
        """The html for the current rendered_html() read: the next html_sequence
        entry (sticking on the last) if a sequence is configured, else self.html."""
        if self.html_sequence:
            idx = min(self._html_idx, len(self.html_sequence) - 1)
            self._html_idx += 1
            return self.html_sequence[idx]
        return self.html

    def _selector_satisfied(self, selector, html=None):
        """Approximate Playwright wait_for_selector against the canned html, for
        the Delta selectors the adapter waits on. Lets selector-based renders
        (chunk 4h) be exercised without a real browser."""
        s = selector or ""
        html = (self.html if html is None else html) or ""
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
        html = self._current_html()
        if wait_for_text is not None:
            satisfied = wait_for_text in (html or "")
        elif wait_for_selector is not None:
            satisfied = (
                wait_for_selector in self.present_selectors
                or self._selector_satisfied(wait_for_selector, html)
            )
        else:
            satisfied = True
        return RenderedPage(
            html=html, wait_satisfied=satisfied, current_url=self.current_url
        )

    async def fill(self, slug, selector, value):
        self.fills.append((selector, value))
        return {"ok": True}

    async def click(self, slug, selector):
        self.clicks.append(selector)
        return {"ok": True, "current_url": self.current_url}

    async def click_download(self, slug, selector, dest_filename=None):
        # The adapter must NOT use the single-selector click-download path.
        self.click_downloads.append(selector)
        raise AssertionError("click_download must not be used by the Delta adapter")

    async def click_download_in_row(
        self, slug, *, table_selector, row_index, menu_trigger_selector,
        download_item_text, timeout_ms=30000,
    ):
        """Simulate the bridge's per-row action-menu download. Records the call,
        fails configured rows (raising like the real 502), and otherwise writes a
        file into the fake bridge download dir and returns its descriptor."""
        self.row_downloads.append(
            {"table": table_selector, "row_index": row_index,
             "trigger": menu_trigger_selector, "text": download_item_text}
        )
        if row_index in self.fail_rows:
            raise BridgeError(f"no download captured for row {row_index}")
        suggested = self.row_filenames.get(row_index, f"document_{row_index}.pdf")
        content = self.row_contents.get(row_index, b"%PDF row " + str(row_index).encode())
        saved = re.sub(
            r"[^A-Za-z0-9._-]", "_", f"r{row_index}_{suggested or 'download.bin'}"
        )
        p = Path(self.download_dir) / saved
        p.write_bytes(content)
        return BridgeRowDownload(
            path=saved, suggested_filename=suggested,
            size_bytes=p.stat().st_size, mime_type=self.row_mime,
        )

    async def select_option(self, slug, selector, value=None, label=None, index=None):
        self.selects.append((selector, {"value": value, "label": label, "index": index}))
        return {"ok": True}

    async def element_exists(self, slug, selector):
        return selector in self.present_selectors

    async def find_links(self, slug, pattern):
        rx = re.compile(pattern, re.IGNORECASE)
        return [u for u in self.links if rx.search(u)]

    async def download(self, slug, url, dest_filename=None):
        # The adapter must NOT use the link-scraping direct-GET path any more.
        raise AssertionError("link-scraping download must not be used by the Delta adapter")

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
async def test_is_authenticated_probes_main_menu():
    # The probe must navigate to the CONFIRMED authenticated landing
    # (/delta/mainMenu.html) — the same page the local capture helper verifies —
    # so capture and /session/test agree on what "logged in" means.
    bridge = FakeBridge(
        current_url="https://www.delta-esourcing.com/delta/mainMenu.html",
        present_selectors={DELTA_SELECTORS["logged_in_marker"]},
    )
    assert await DeltaEsourcingAdapter().is_authenticated(_ctx(bridge)) is True
    assert any("mainMenu.html" in u for u in bridge.navigated)
    assert DELTA_URLS["main_menu"].endswith("/delta/mainMenu.html")


def test_login_success_pattern_matches_mainmenu_not_login():
    # Matches the real logged-in landing; rejects the login page; does NOT
    # require /delta/suppliers/.
    rx = re.compile(DELTA_LOGIN_SUCCESS_PATTERN)
    assert rx.search("https://www.delta-esourcing.com/delta/mainMenu.html")
    assert rx.search(
        "https://www.delta-esourcing.com/delta/suppliers/select/addToList.html"
    )
    assert not rx.search("https://www.delta-esourcing.com/delta/login.html")


# --- frame-aware probe (port of the #92 helper fixes into the backend) ------


def test_split_or_selectors_top_level_only():
    # Splits at top-level commas; preserves commas inside :has-text('...').
    parts = _split_or_selectors(
        "a:has-text('Response Manager'), header:has-text('A, B'), #x"
    )
    assert parts == [
        "a:has-text('Response Manager')",
        "header:has-text('A, B')",
        "#x",
    ]


def test_logged_in_markers_split_from_selector():
    # Each alternative of the real marker is available independently.
    assert "a:has-text('Response Manager')" in DELTA_LOGGED_IN_MARKERS
    assert any("Supplier Administrator" in m for m in DELTA_LOGGED_IN_MARKERS)
    assert len(DELTA_LOGGED_IN_MARKERS) >= 4


class _RichBridge:
    """A bridge exposing the frame-aware `logged_in_marker_report`. Tracks
    whether the rich path (not rendered_html) was used."""

    def __init__(self, *, current_url: str, report: dict | None = None) -> None:
        self._current_url = current_url
        self._report = report
        self.report_called = False
        self.rendered_called = False
        self.navigated: list[str] = []

    async def navigate(self, slug, url):
        self.navigated.append(url)
        return {"current_url": self._current_url}

    async def session_status(self, slug):
        return {"current_url": self._current_url}

    async def logged_in_marker_report(self, slug, selectors, timeout_ms=8000):
        self.report_called = True
        return self._report

    async def rendered_html(self, slug, **kwargs):
        self.rendered_called = True
        return RenderedPage(html="", wait_satisfied=False,
                            current_url=self._current_url)


@pytest.mark.asyncio
async def test_login_diagnostics_uses_frame_aware_report_when_present():
    report = {
        "title": "Activity Centre | Delta",
        "current_url": "https://www.delta-esourcing.com/delta/mainMenu.html",
        "frames_checked": 2,
        "any_visible": True,
        "markers": [
            {"selector": "a:has-text('Response Manager')", "found": True,
             "frame": "main", "matches": 1, "error": None},
        ],
    }
    bridge = _RichBridge(
        current_url="https://www.delta-esourcing.com/delta/mainMenu.html",
        report=report,
    )
    diag = await DeltaEsourcingAdapter().login_diagnostics(_ctx(bridge))
    assert bridge.report_called is True
    assert bridge.rendered_called is False  # rich path, not the old wait
    assert diag["logged_in"] is True
    assert diag["title"] == "Activity Centre | Delta"
    assert diag["frames_checked"] == 2
    assert diag["markers"][0]["found"] is True
    # is_authenticated is the boolean view of the same path.
    assert await DeltaEsourcingAdapter().is_authenticated(_ctx(bridge)) is True


@pytest.mark.asyncio
async def test_login_diagnostics_not_logged_in_reports_markers():
    report = {
        "title": "Delta eSourcing",
        "current_url": "https://www.delta-esourcing.com/delta/mainMenu.html",
        "frames_checked": 1,
        "any_visible": False,
        "markers": [
            {"selector": "a:has-text('Response Manager')", "found": False,
             "frame": None, "matches": 0, "error": None},
        ],
    }
    bridge = _RichBridge(
        current_url="https://www.delta-esourcing.com/delta/mainMenu.html",
        report=report,
    )
    diag = await DeltaEsourcingAdapter().login_diagnostics(_ctx(bridge))
    assert diag["logged_in"] is False
    assert diag["markers"][0]["found"] is False
    assert diag["title"] == "Delta eSourcing"


@pytest.mark.asyncio
async def test_login_diagnostics_redirected_to_login_skips_marker_report():
    bridge = _RichBridge(
        current_url="https://www.delta-esourcing.com/delta/login.html",
    )
    diag = await DeltaEsourcingAdapter().login_diagnostics(_ctx(bridge))
    assert diag["logged_in"] is False
    assert diag["redirected_to_login"] is True
    assert bridge.report_called is False  # short-circuit before the marker check


@pytest.mark.asyncio
async def test_login_diagnostics_falls_back_without_rich_method():
    # A bridge WITHOUT logged_in_marker_report (e.g. HTTP) still works via the
    # original rendered_html wait — existing behaviour is preserved.
    bridge = FakeBridge(
        current_url="https://www.delta-esourcing.com/delta/mainMenu.html",
        present_selectors={DELTA_SELECTORS["logged_in_marker"]},
    )
    diag = await DeltaEsourcingAdapter().login_diagnostics(_ctx(bridge))
    assert diag["logged_in"] is True


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


# --- download: per-row action-menu click-download (chunk 4i) -----------


@pytest.mark.asyncio
async def test_download_documents_via_row_clicks(tmp_path, monkeypatch):
    bridge_dl, storage = _patch_storage(monkeypatch, tmp_path)
    bridge = FakeBridge(
        html=_docs_table_html(n=3),
        current_url=STAGE_ONE_URL,  # resolve respId/listId from here
        present_selectors={DELTA_SELECTORS["document_rows"]},
        download_dir=str(bridge_dl),
    )
    res = await DeltaEsourcingAdapter().download_documents(_ctx(bridge), "ignored")
    assert res.status == DownloadStatus.complete
    assert len(res.files) == 3
    # Navigated to Stage One; downloaded each row via the action-menu click —
    # NEVER the old link-scraping (bridge.download/click_download would assert).
    assert any("suppRespStatus.html?id=555&listId=777" in u for u in bridge.navigated)
    assert bridge.click_downloads == []
    assert bridge.clicks == []
    # Iterated per data row 0..2 through click_download_in_row, targeting the
    # confirmed table id and the ⋮/"Download File" selectors.
    assert [r["row_index"] for r in bridge.row_downloads] == [0, 1, 2]
    assert all(r["table"] == "table#document" for r in bridge.row_downloads)
    # The ⋮ trigger is the confirmed Font Awesome icon, the item the body popup.
    assert all(
        r["trigger"] == "i.bip-actions-menu-link.fa-ellipsis-v"
        for r in bridge.row_downloads
    )
    assert all(r["text"] == "Download File" for r in bridge.row_downloads)
    for f in res.files:
        # The doc belongs to the Stage One page; there is no per-doc URL.
        assert "suppRespStatus.html" in f.url
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
    assert len(bridge.row_downloads) == 22


@pytest.mark.asyncio
async def test_download_uses_precaptured_ids_and_maximises_page_size(
    tmp_path, monkeypatch
):
    bridge_dl, _ = _patch_storage(monkeypatch, tmp_path)
    bridge = FakeBridge(
        html=_docs_table_html(n=2),
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
    # Only the first 3 rows were clicked; rows beyond the cap are not fetched.
    assert [r["row_index"] for r in bridge.row_downloads] == [0, 1, 2]


@pytest.mark.asyncio
async def test_download_dedups_identical_files(tmp_path, monkeypatch):
    bridge_dl, _ = _patch_storage(monkeypatch, tmp_path)
    # Every row's click yields byte-identical content -> one kept after dedup.
    bridge = FakeBridge(
        html=_docs_table_html(n=3), current_url=STAGE_ONE_URL,
        download_dir=str(bridge_dl),
        row_contents=dict.fromkeys(range(3), b"%PDF identical"),
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
        html="<table id='document'><tr><th>Document Title</th></tr></table>",
        current_url=STAGE_ONE_URL, download_dir=str(bridge_dl),
    )
    res = await DeltaEsourcingAdapter().download_documents(_ctx(bridge), "ignored")
    assert res.status == DownloadStatus.nothing_available
    # Not a silent empty result — a clear detail naming the likely cause.
    assert "did not render" in res.detail
    # Zero rows seen → no per-row clicks attempted.
    assert bridge.row_downloads == []


@pytest.mark.asyncio
async def test_download_reads_rendered_dom_not_page_html(tmp_path, monkeypatch):
    # The rows live only in the RENDERED DOM (bip-table JS render), so the adapter
    # must read via rendered_html — never the raw page-html shell.
    bridge_dl, _ = _patch_storage(monkeypatch, tmp_path)
    bridge = FakeBridge(
        html=_docs_table_html(n=3),
        current_url=STAGE_ONE_URL,
        present_selectors={DELTA_SELECTORS["document_rows"]},
        download_dir=str(bridge_dl),
    )
    res = await DeltaEsourcingAdapter().download_documents(_ctx(bridge), "ignored")
    assert res.status == DownloadStatus.complete
    assert len(res.files) == 3
    # Rendered DOM read with a wait for the documents-table data-row selector;
    # page_html unused.
    assert bridge.rendered_calls
    assert bridge.rendered_calls[0]["selector"] == DELTA_SELECTORS["document_rows"]
    assert bridge.page_html_calls == 0


@pytest.mark.asyncio
async def test_download_does_not_use_link_scraping(tmp_path, monkeypatch):
    # Belt-and-braces: the adapter iterates rows via click_download_in_row and
    # never touches a downloadDocument link / docId path (bridge.download and
    # bridge.find_links must not drive the download).
    bridge_dl, _ = _patch_storage(monkeypatch, tmp_path)
    bridge = FakeBridge(
        html=_docs_table_html(n=4),
        current_url=STAGE_ONE_URL,
        download_dir=str(bridge_dl),
    )
    res = await DeltaEsourcingAdapter().download_documents(_ctx(bridge), "ignored")
    assert res.status == DownloadStatus.complete
    assert [r["row_index"] for r in bridge.row_downloads] == [0, 1, 2, 3]
    assert all("downloadDocument" not in f.url for f in res.files)


@pytest.mark.asyncio
async def test_download_partial_when_some_rows_fail(tmp_path, monkeypatch):
    # Row 1's click yields no download → partial: the other rows are saved, the
    # failed row is counted missing and logged per-row.
    bridge_dl, _ = _patch_storage(monkeypatch, tmp_path)
    bridge = FakeBridge(
        html=_docs_table_html(n=3, titles=["A.pdf", "B.pdf", "C.pdf"]),
        current_url=STAGE_ONE_URL,
        download_dir=str(bridge_dl),
        fail_rows={1},
    )
    res = await DeltaEsourcingAdapter().download_documents(_ctx(bridge), "ignored")
    assert res.status == DownloadStatus.partial
    assert len(res.files) == 2
    assert len(res.missing) == 1
    # The failed row is identified by its title.
    assert res.missing == ["B.pdf"]


@pytest.mark.asyncio
async def test_download_error_when_all_rows_fail(tmp_path, monkeypatch):
    # Rows were seen but EVERY click failed → a clear error, NOT a silent
    # nothing_available.
    bridge_dl, _ = _patch_storage(monkeypatch, tmp_path)
    bridge = FakeBridge(
        html=_docs_table_html(n=3),
        current_url=STAGE_ONE_URL,
        download_dir=str(bridge_dl),
        fail_rows={0, 1, 2},
    )
    res = await DeltaEsourcingAdapter().download_documents(_ctx(bridge), "ignored")
    assert res.status == DownloadStatus.error
    assert res.status != DownloadStatus.nothing_available
    assert res.files == []
    assert len(res.missing) == 3
    assert "could not trigger any" in res.detail
    assert "3 documents" in res.detail


@pytest.mark.asyncio
async def test_download_extension_falls_back_to_file_type(tmp_path, monkeypatch):
    # The captured download has no extension and an unknown MIME → the row's
    # File Type column ("PDF") supplies the extension.
    bridge_dl, storage = _patch_storage(monkeypatch, tmp_path)
    bridge = FakeBridge(
        html=_docs_table_html(n=1, titles=["Specification"], file_types=["PDF"]),
        current_url=STAGE_ONE_URL,
        download_dir=str(bridge_dl),
        row_filenames={0: "Specification"},  # no extension on the download
        row_mime="application/octet-stream",  # unmapped MIME
    )
    res = await DeltaEsourcingAdapter().download_documents(_ctx(bridge), "ignored")
    assert res.status == DownloadStatus.complete
    assert len(res.files) == 1
    f = res.files[0]
    # Extension came from the File Type column.
    assert f.filename == "Specification.pdf"
    assert f.storage_key.endswith(".pdf")
    assert (storage / f.storage_key).is_file()


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


@pytest.mark.asyncio
async def test_download_maximises_page_size_and_polls_until_all_rows(
    tmp_path, monkeypatch
):
    # Live bug 4j: the page-size wasn't maximised, so only 10 of 22 rows rendered.
    # The length select is present → maximise; the grid then re-renders with all
    # 22 rows on a LATER read → the adapter polls until it sees them, downloads 22.
    bridge_dl, _ = _patch_storage(monkeypatch, tmp_path)
    bridge = FakeBridge(
        current_url=STAGE_ONE_URL,
        download_dir=str(bridge_dl),
        present_selectors={
            DELTA_SELECTORS["page_size_select"],
            DELTA_SELECTORS["document_rows"],
        },
        # First read = 10 rows (default page), then the maximised grid re-renders
        # with all 22; "of 22 items" is shown throughout.
        html_sequence=[
            _docs_table_html(n=10, items=22),
            _docs_table_html(n=22, items=22),
            _docs_table_html(n=22, items=22),
        ],
    )
    res = await DeltaEsourcingAdapter().download_documents(_ctx(bridge), "ignored")
    assert res.status == DownloadStatus.complete
    assert len(res.files) == 22
    assert len(bridge.row_downloads) == 22
    # The DataTables length select was maximised to its largest option.
    assert bridge.selects and bridge.selects[0][1]["index"] == -1
    assert bridge.selects[0][0] == DELTA_SELECTORS["page_size_select"]


@pytest.mark.asyncio
async def test_download_partial_when_select_missing_and_rows_short(
    tmp_path, monkeypatch
):
    # The length select is NOT present → maximise is skipped (logged fallback);
    # only 10 of 22 rows render and never grow → partial, not a hang.
    bridge_dl, _ = _patch_storage(monkeypatch, tmp_path)
    bridge = FakeBridge(
        html=_docs_table_html(n=10, items=22),
        current_url=STAGE_ONE_URL,
        download_dir=str(bridge_dl),
    )
    res = await DeltaEsourcingAdapter().download_documents(_ctx(bridge), "ignored")
    assert res.status == DownloadStatus.partial
    assert len(res.files) == 10
    # No page-size select was found, so none was set.
    assert bridge.selects == []
    assert res.detail and "10 of 22" in res.detail


@pytest.mark.asyncio
async def test_download_assigns_distinct_per_doc_urls(tmp_path, monkeypatch):
    # Chunk 4k regression: every file must carry a DISTINCT url so they don't
    # collide on the tender_document_files (tender_id, url) unique constraint and
    # collapse to one DB row. Delta has no per-doc URL, so the adapter derives a
    # unique, non-null one from the Stage One URL + the row title.
    bridge_dl, _ = _patch_storage(monkeypatch, tmp_path)
    bridge = FakeBridge(
        html=_docs_table_html(n=3, titles=["A.pdf", "B.pdf", "C.pdf"]),
        current_url=STAGE_ONE_URL,
        download_dir=str(bridge_dl),
    )
    res = await DeltaEsourcingAdapter().download_documents(_ctx(bridge), "ignored")
    assert res.status == DownloadStatus.complete
    urls = [f.url for f in res.files]
    # Three documents -> three DISTINCT, non-null urls.
    assert len(urls) == 3
    assert all(urls)
    assert len(set(urls)) == 3
    # Each is rooted at the Stage One URL (so it stays a delta-esourcing.com ref).
    assert all(u.startswith(STAGE_ONE_URL) for u in urls)
    assert all("suppRespStatus.html" in u for u in urls)


def test_action_menu_selectors_are_confirmed():
    # Pin the live-confirmed Stage One selectors (chunk 4j) so a regression in
    # any of them is caught here, not only on a live run.
    assert DELTA_SELECTORS["documents_table"] == "table#document"
    assert DELTA_SELECTORS["document_rows"] == "table#document tbody tr[role='row']"
    assert DELTA_SELECTORS["row_action_menu"] == "i.bip-actions-menu-link.fa-ellipsis-v"
    assert DELTA_SELECTORS["row_download_item_text"] == "Download File"
    # The page-size selector covers the DataTables length menu.
    assert "_length" in DELTA_SELECTORS["page_size_select"]


def test_stage_one_url_is_well_formed():
    assert DELTA_URLS["stage_one"] % ("555", "777") == STAGE_ONE_URL
    # The dead link-scraping download URL has been removed.
    assert "document_download" not in DELTA_URLS
