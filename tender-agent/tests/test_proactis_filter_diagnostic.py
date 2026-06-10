"""Proactis filter-diagnostic — capture WHAT the filter panel + category
popup actually contain, all offline.

A fake bridge models the LIVE popup shapes (2026-06-10 follow-up capture)
so the snapshot is proven to read them the way the discovery run does:

  - Category popup: the code probe's tree holds `45000000-7 - Construction
    work` while `#divNoSearchResults` is present-but-HIDDEN — the probe must
    report matched=True (the old presence-based check misread exactly this
    as a miss). A genuinely empty search (displayed marker, zero nodes)
    still reports the miss.
  - Portal control: the panel's single-value Portals <select>; the snapshot
    reports its presence + REAL option labels.

No Playwright, no network. Secrets (password, cookie values) never appear
in the snapshot or the logs.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from tender_agent.api.deps import current_account
from tender_agent.main import app
from tender_agent.services.bridge_client import BridgeError
from tender_agent.services.discovery.proactis_filter_diagnostic import (
    CATEGORY_PROBE_CODE,
    CATEGORY_PROBE_WORD,
    HTML_EXCERPT_CHARS,
    capture_filter_state,
    run_filter_diagnostic,
)
from tender_agent.services.portals.base import Credentials
from tests.conftest import load_text_fixture

_PASSWORD = "SuperSecret123!"
_CREDS = Credentials(
    username="ops@genera-systems.com",
    password=_PASSWORD,
    email="ops@genera-systems.com",
)

# Real-shape mock data. The code probe returns the CAPTURED live popup:
# ONE construction node + a hidden no-results marker (the shape the old
# detection misread). The word probe returns a plain populated tree.
_LIVE_CPV_POPUP_HTML = load_text_fixture("proactis_cpv_popup_live.html")
_CONSTRUCTION_TREE_HTML = (
    '<div id="DivTree"><ul class="dynatree-container">'
    '<li id="node~CPV~45000000-7"><span class="dynatree-node">'
    '<span class="dynatree-checkbox">&nbsp;</span>'
    '<a class="dynatree-title">45000000-7 - Construction work</a>'
    "</span></li></ul></div>"
)
_EMPTY_TREE_HTML = (
    '<div id="DivTree"><ul class="dynatree-container"></ul></div>'
    '<div id="divNoSearchResults">No matching categories found.</div>'
)
_PANEL_HTML = (
    '<form id="opportunitiesForm" action="/Opportunities/Index">'
    '  <label>Keywords <input id="Keywords"/></label>'
    '  <button type="button" id="btnAddCpv">Add CPV</button>'
    '  <button type="button" id="btnAddPortal">'
    '    Select organisations / portals'
    "  </button>"
    "</form>"
)


class _FilterBridge:
    """Async fake covering the bridge surface the filter diagnostic uses."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.fills: list[tuple[str, str]] = []
        self.popup_open = False
        self.search_term: str | None = None
        # The real page HAS the portal select; a test can flip this to
        # model a tenant where the control moved.
        self.portal_select_present = True

    async def bridge_available(self) -> bool:
        return True

    async def open_session(self, slug, start_url=None) -> dict:
        self.events.append(("open_session", slug))
        return {}

    async def close_session(self, slug) -> dict:
        self.events.append(("close_session", slug))
        return {"closed": True}

    async def navigate(self, slug, url) -> dict:
        self.events.append(("navigate", url))
        return {"current_url": url, "status_code": 200}

    async def session_status(self, _slug) -> dict:
        return {
            "current_url": (
                "https://procontract.due-north.com/Opportunities/Index"
                "?tabName=opportunities"
            ),
            "last_status_code": 200,
        }

    async def click(self, _slug, selector) -> dict:
        self.events.append(("click", selector))
        if "Add" in selector or "btnAddCpv" in selector:
            # Category popup opens on the trigger our flow uses; the new
            # Portals trigger we DON'T know yet returns no click here.
            self.popup_open = True
        return {"ok": True}

    async def fill(self, _slug, selector, value) -> dict:
        self.fills.append((selector, value))
        self.events.append(("fill", value))
        if "TxtFilterNodes" in selector and value:
            self.search_term = value
        return {"ok": True}

    async def element_exists(self, _slug, selector) -> bool:
        if "TxtFilterNodes" in selector:
            return self.popup_open
        # The portal dropdown (single-value, "All" default).
        if "PortalWithAllOption" in selector:
            return self.portal_select_present
        return False

    async def rendered_html(self, _slug, *, wait_for_selector=None, **_kw):
        from tender_agent.services.bridge_client import RenderedPage

        # The code probe sees the CAPTURED live shape: tree filtered to the
        # construction node, marker present but HIDDEN. The word probe sees
        # a plain populated tree.
        html = (
            _LIVE_CPV_POPUP_HTML
            if self.search_term == CATEGORY_PROBE_CODE
            else _CONSTRUCTION_TREE_HTML
        )
        return RenderedPage(html=html, wait_satisfied=True, current_url=None)

    async def page_text(self, _slug) -> str:
        return (
            "Find Opportunities. Narrow your results. Keywords. Categories. "
            "Add CPV. Select organisations / portals. Update."
        )

    async def screenshot(self, _slug, label="screenshot") -> dict:
        return {"path": f"{label}.png", "size_bytes": 1}

    async def evaluate(self, _slug, script):
        if "PortalWithAllOption" in script:
            if not self.portal_select_present:
                return None
            return [
                {"label": "All", "value": ""},
                {"label": "London Tenders", "value": "g-1"},
                {"label": "South East Business Portal", "value": "g-2"},
                {"label": "EastMidsTenders", "value": "g-3"},
            ]
        if "document.title" in script:
            return "Find Opportunities | ProContract"
        if "buttons, anchor" in script or "querySelectorAll" in script:
            return [
                {
                    "tag": "button",
                    "text": "Add CPV",
                    "id": "btnAddCpv",
                    "classes": "btn",
                    "name": None,
                },
                {
                    "tag": "button",
                    "text": "Select organisations / portals",
                    "id": "btnAddPortal",
                    "classes": "btn",
                    "name": None,
                },
                {
                    "tag": "button",
                    "text": "Update",
                    "id": "btnUpdate",
                    "classes": "btn btn-primary",
                    "name": None,
                },
            ]
        # _POPUP_HTML_JS / _PANEL_HTML_JS — return matching outerHTML.
        if "DivTree" in script:
            if self.popup_open and self.search_term == CATEGORY_PROBE_CODE:
                return _LIVE_CPV_POPUP_HTML
            if self.popup_open and self.search_term == CATEGORY_PROBE_WORD:
                return _CONSTRUCTION_TREE_HTML
            if self.popup_open:
                return '<div id="DivTree"></div>'
            return ""
        if "opportunitiesForm" in script or "outerHTML" in script:
            return _PANEL_HTML
        return None


class _LoggedInBridge(_FilterBridge):
    """Adds the surface `run_filter_diagnostic` calls — its login wrapper
    flows through `login_with_credentials`, which probes the logged-in
    marker once the form is submitted."""

    async def element_exists(self, slug, selector):
        # Whatever the login flow probes after submit looks for one of the
        # logged-in-marker alternatives — return True for any of them so
        # the login path returns ok and the diagnostic moves on to the
        # filter panel.
        if (
            "Find opportunities" in selector
            or "My activities" in selector
            or "Supplier" in selector
        ):
            return True
        return await super().element_exists(slug, selector)


# --- capture_filter_state -----------------------------------------------------


@pytest.mark.asyncio
async def test_capture_reads_hidden_marker_search_as_matched():
    """THE detection fix: the live capture showed the code probe's tree
    holding `45000000-7 - Construction work` while the (permanently-in-DOM)
    no-results marker sat hidden beside it — and the old presence-based
    check reported matched=False. The probe must now report the hit."""
    bridge = _FilterBridge()
    with capture_logs() as logs:
        diag = await capture_filter_state(bridge, "procontract")
    assert diag.category_popup is not None
    probes = {p.term: p for p in diag.category_popup.probes}
    code_probe = probes[CATEGORY_PROBE_CODE]
    assert code_probe.matched is True
    assert code_probe.no_results_marker_visible is False
    assert code_probe.tree_status == "results"
    # The excerpt shows the operator the real node markup (hidden marker
    # included — proof of the shape this fix reads correctly).
    assert "45000000-7 - Construction work" in code_probe.nodes_excerpt
    # The descriptive word keeps returning nodes too.
    assert probes[CATEGORY_PROBE_WORD].matched is True
    assert probes[CATEGORY_PROBE_WORD].tree_status == "results"
    assert "dynatree-title" in probes[CATEGORY_PROBE_WORD].nodes_excerpt

    # One info log with the headline counters — no secrets.
    events = [e for e in logs if e["event"] == "discovery.proactis.filter_diagnostic"]
    assert len(events) == 1
    assert events[0]["category_probes_matched"] == 2


@pytest.mark.asyncio
async def test_capture_still_reports_a_genuine_miss():
    """A DISPLAYED marker with an empty tree stays a miss — the fix must
    not flip every search to matched."""

    class _MissBridge(_FilterBridge):
        async def rendered_html(self, _slug, *, wait_for_selector=None, **_kw):
            from tender_agent.services.bridge_client import RenderedPage

            html = (
                _EMPTY_TREE_HTML
                if self.search_term == CATEGORY_PROBE_CODE
                else _CONSTRUCTION_TREE_HTML
            )
            return RenderedPage(
                html=html, wait_satisfied=True, current_url=None
            )

    diag = await capture_filter_state(_MissBridge(), "procontract")
    probes = {p.term: p for p in diag.category_popup.probes}
    assert probes[CATEGORY_PROBE_CODE].matched is False
    assert probes[CATEGORY_PROBE_CODE].no_results_marker_visible is True
    assert probes[CATEGORY_PROBE_CODE].tree_status == "no_results"
    assert probes[CATEGORY_PROBE_WORD].matched is True


@pytest.mark.asyncio
async def test_probe_waits_out_loading_then_reports_results(monkeypatch):
    """The tree loads asynchronously — the probe's settle loop must re-read
    past the Loading… status row instead of judging mid-flight. Pacing is
    late-bound off proactis_dynatree, so the same monkeypatch the driver
    tests use retunes the diagnostic too."""
    from tender_agent.services.discovery import proactis_dynatree

    monkeypatch.setattr(proactis_dynatree, "TREE_SETTLE_RETRY_DELAY_S", 0.01)

    loading_html = _LIVE_CPV_POPUP_HTML.replace(
        '<li id="node~CPV~45000000-7">',
        '<li><span class="dynatree-node dynatree-statusnode-wait">'
        '<a class="dynatree-title">Loading&#8230;</a></span></li>'
        '<li id="node~CPV~45000000-7" style="display:none">',
    )

    class _SlowTreeBridge(_FilterBridge):
        def __init__(self) -> None:
            super().__init__()
            self.reads = 0

        async def rendered_html(self, _slug, *, wait_for_selector=None, **_kw):
            from tender_agent.services.bridge_client import RenderedPage

            self.reads += 1
            html = loading_html if self.reads == 1 else _LIVE_CPV_POPUP_HTML
            return RenderedPage(
                html=html, wait_satisfied=True, current_url=None
            )

    bridge = _SlowTreeBridge()
    diag = await capture_filter_state(bridge, "procontract")
    probes = {p.term: p for p in diag.category_popup.probes}
    assert probes[CATEGORY_PROBE_CODE].matched is True
    assert probes[CATEGORY_PROBE_CODE].tree_status == "results"
    # The loop genuinely re-read (first read was the loading state).
    assert bridge.reads >= 3


@pytest.mark.asyncio
async def test_capture_probes_portal_select_and_lists_its_options():
    """The 2026-06-10 capture proved Portals is a single-value dropdown —
    the snapshot now reports the select's presence + its REAL option labels
    so PROACTIS_DISCOVERY_PORTALS can be trued up against them."""
    bridge = _FilterBridge()
    diag = await capture_filter_state(bridge, "procontract")
    panel = diag.portal_control
    assert panel is not None
    assert panel.portal_select_present is True
    assert "PortalWithAllOptionFilter" in panel.portal_select_selector
    assert "London Tenders" in panel.portal_options
    assert "EastMidsTenders" in panel.portal_options
    # The coarse control scan + panel HTML stay, for the next control move.
    texts = [c.text for c in panel.panel_controls]
    assert "Select organisations / portals" in texts
    assert "Select organisations / portals" in panel.panel_html_excerpt


@pytest.mark.asyncio
async def test_capture_flags_missing_portal_select():
    """A tenant where the select moved → present=False, options empty —
    loud in the snapshot rather than silently wrong."""
    bridge = _FilterBridge()
    bridge.portal_select_present = False
    diag = await capture_filter_state(bridge, "procontract")
    assert diag.portal_control.portal_select_present is False
    assert diag.portal_control.portal_options == []


@pytest.mark.asyncio
async def test_html_excerpts_are_capped():
    bridge = _FilterBridge()
    # Stretch the response so the cap actually engages.
    huge = "<x/>" * 5000
    original_evaluate = bridge.evaluate

    async def _evaluate(slug, script):
        if "outerHTML" in script:
            return huge
        return await original_evaluate(slug, script)

    bridge.evaluate = _evaluate  # type: ignore[assignment]
    diag = await capture_filter_state(bridge, "procontract")
    assert len(diag.portal_control.panel_html_excerpt) <= HTML_EXCERPT_CHARS


@pytest.mark.asyncio
async def test_capture_survives_broken_reads():
    """Filter diagnostic runs against an already-misbehaving page; a broken
    read goes into capture_error, never raised."""

    class _BrokenBridge(_FilterBridge):
        async def navigate(self, *_a, **_kw):
            raise BridgeError("nav timeout")

        async def click(self, *_a, **_kw):
            raise BridgeError("click timeout")

        async def session_status(self, _slug):
            raise BridgeError("status gone")

    diag = await capture_filter_state(_BrokenBridge(), "procontract")
    assert diag.capture_error is not None
    # Snapshots still present, with their own errors recorded.
    assert diag.category_popup is not None
    assert diag.category_popup.error is not None


@pytest.mark.asyncio
async def test_no_secrets_in_snapshot_or_logs():
    bridge = _LoggedInBridge()
    with capture_logs() as logs:
        result = await run_filter_diagnostic(
            credentials=_CREDS, slug="procontract", bridge=bridge
        )
    dumped = json.dumps(result, default=str)
    log_dump = json.dumps(logs, default=str)
    assert _PASSWORD not in dumped
    assert _PASSWORD not in log_dump
    assert "set-cookie" not in dumped.lower()


# --- admin endpoint ---------------------------------------------------------


@pytest.fixture()
def anon_client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def auth_client():
    app.dependency_overrides[current_account] = lambda: object()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(current_account, None)


def test_endpoint_rejects_anonymous(anon_client: TestClient) -> None:
    resp = anon_client.post("/admin/portals/proactis/filter-diagnostic")
    assert resp.status_code == 401


def test_endpoint_returns_snapshot_json(auth_client, monkeypatch) -> None:
    from cryptography.fernet import Fernet

    from tender_agent.services import bridge_client as bridge_client_mod
    from tender_agent.services import credentials as creds_mod
    from tests._billing_fixtures import make_engine_and_session

    store = creds_mod.CredentialsStore(
        session_factory=make_engine_and_session()[1],
        encryption_key=Fernet.generate_key().decode(),
    )
    store.store_credentials(348, "eduard", _CREDS, platform_slug="proactis")
    monkeypatch.setattr(creds_mod, "_store", store)
    monkeypatch.setattr(
        bridge_client_mod, "make_bridge_client", lambda: _LoggedInBridge()
    )

    resp = auth_client.post("/admin/portals/proactis/filter-diagnostic")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["logged_in"] is True
    assert body["category_popup"]["opened"] is True
    probes = {p["term"]: p for p in body["category_popup"]["probes"]}
    # The live shape (node + hidden marker) reads as a HIT end-to-end.
    assert probes[CATEGORY_PROBE_CODE]["matched"] is True
    assert probes[CATEGORY_PROBE_CODE]["tree_status"] == "results"
    assert probes[CATEGORY_PROBE_WORD]["matched"] is True
    assert body["portal_control"]["portal_select_present"] is True
    assert "London Tenders" in body["portal_control"]["portal_options"]
    assert any(
        c["text"] == "Select organisations / portals"
        for c in body["portal_control"]["panel_controls"]
    )
    assert _PASSWORD not in resp.text


def test_endpoint_404_when_no_credentials_stored(auth_client, monkeypatch) -> None:
    from cryptography.fernet import Fernet

    from tender_agent.services import credentials as creds_mod
    from tests._billing_fixtures import make_engine_and_session

    empty_store = creds_mod.CredentialsStore(
        session_factory=make_engine_and_session()[1],
        encryption_key=Fernet.generate_key().decode(),
    )
    monkeypatch.setattr(creds_mod, "_store", empty_store)
    resp = auth_client.post("/admin/portals/proactis/filter-diagnostic")
    assert resp.status_code == 404
    assert "no_proactis_credentials_stored" in resp.json()["detail"]
