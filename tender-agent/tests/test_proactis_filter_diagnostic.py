"""Proactis filter-diagnostic — capture WHAT the filter panel + category
popup actually contain, all offline.

A fake bridge models the two confirmed live failures so the snapshot is
proven to surface what the operator needs to see for a real fix:

  - Category popup: prefix '45' lands on `divNoSearchResults`; the word
    'construction' returns nodes (the working state Proactis indexes).
  - Portal control: the current "Add new portal" / "Add portal" trigger
    selector misses; the panel's JS-enumerated controls include the REAL
    Portals trigger so the operator can read its actual label.

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

_PASSWORD = "SuperSecret123!"
_CREDS = Credentials(
    username="ops@genera-systems.com",
    password=_PASSWORD,
    email="ops@genera-systems.com",
)

# Real-shape mock data — the strings reflect what the operator would see on
# a live capture. The 45 probe lands on the no-results marker; the word
# probe returns construction-family CPV nodes.
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
        # When True, element_exists matches the OLD portal-trigger selector
        # — flip in the "before fix" test to assert the diagnostic notices.
        self.current_portal_trigger_matches = False

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
        if "divNoSearchResults" in selector:
            return self.search_term == CATEGORY_PROBE_CODE
        # The current (broken) Portals trigger.
        if "Add new portal" in selector or "Add portal" in selector:
            return self.current_portal_trigger_matches
        return False

    async def rendered_html(self, _slug, *, wait_for_selector=None, **_kw):
        from tender_agent.services.bridge_client import RenderedPage

        html = (
            _EMPTY_TREE_HTML
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
                return _EMPTY_TREE_HTML
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
async def test_capture_records_category_miss_for_prefix_and_hit_for_word():
    bridge = _FilterBridge()
    with capture_logs() as logs:
        diag = await capture_filter_state(bridge, "procontract")
    assert diag.category_popup is not None
    probes = {p.term: p for p in diag.category_popup.probes}
    # The prefix lands on the no-results marker — the live symptom.
    assert probes[CATEGORY_PROBE_CODE].matched is False
    assert probes[CATEGORY_PROBE_CODE].no_results_marker_visible is True
    assert "divNoSearchResults" in probes[CATEGORY_PROBE_CODE].nodes_excerpt
    # The descriptive word returns nodes — proving the gap is the format.
    assert probes[CATEGORY_PROBE_WORD].matched is True
    assert probes[CATEGORY_PROBE_WORD].no_results_marker_visible is False
    assert "45000000" in probes[CATEGORY_PROBE_WORD].nodes_excerpt
    assert "dynatree-title" in probes[CATEGORY_PROBE_WORD].nodes_excerpt

    # One info log with the headline counters — no secrets.
    events = [e for e in logs if e["event"] == "discovery.proactis.filter_diagnostic"]
    assert len(events) == 1
    assert events[0]["category_probes_matched"] == 1


@pytest.mark.asyncio
async def test_capture_records_portal_trigger_missing_and_lists_real_controls():
    bridge = _FilterBridge()  # default: current trigger does NOT match
    diag = await capture_filter_state(bridge, "procontract")
    panel = diag.portal_control
    assert panel is not None
    assert panel.current_trigger_matches is False
    # The REAL control's label sits in the enumerated controls — the
    # whole reason for this diagnostic. The operator reads this back and we
    # wire its real selector.
    texts = [c.text for c in panel.panel_controls]
    assert "Select organisations / portals" in texts
    # And the panel HTML excerpt carries it for the operator to copy/paste.
    assert "Select organisations / portals" in panel.panel_html_excerpt


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
    assert probes[CATEGORY_PROBE_CODE]["matched"] is False
    assert probes[CATEGORY_PROBE_WORD]["matched"] is True
    assert body["portal_control"]["current_trigger_matches"] is False
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
