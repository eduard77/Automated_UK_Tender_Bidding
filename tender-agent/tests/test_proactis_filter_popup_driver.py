"""Dynatree popup orchestrator — drives the bridge against canned popup HTML.

The pure-logic parser is exercised by test_proactis_dynatree.py. This file
covers the orchestration: open → clear → fill → ensure-exact → click search
→ wait → tick checkbox → tick next → click apply. The bridge is a
hand-written async fake recording every call; no Playwright, no network.
"""
from __future__ import annotations

import pytest

from tender_agent.services.bridge_client import BridgeError, RenderedPage
from tender_agent.services.discovery.proactis_discovery import (
    DiscoveryRunResult,
    _apply_filters_from_profile,
    _drive_dynatree_popup,
)
from tender_agent.services.discovery.proactis_filter_config import (
    ProactisFilterConfig,
)

_POPULATED_HTML = """
<div id="DivTree"><ul class="dynatree-container">
  <li id="node~CPV~45000000-7">
    <span class="dynatree-node">
      <span class="dynatree-checkbox">&nbsp;</span>
      <a class="dynatree-title">45000000-7 - Construction work</a>
    </span>
  </li>
  <li id="node~CPV~72000000-5">
    <span class="dynatree-node">
      <span class="dynatree-checkbox">&nbsp;</span>
      <a class="dynatree-title">72000000-5 - IT services</a>
    </span>
  </li>
</ul></div>
"""

_NO_RESULTS_HTML = """
<div id="DivTree"><ul class="dynatree-container"></ul></div>
<div id="divNoSearchResults">No matching categories found.</div>
"""

_REGION_HTML = """
<div id="DivTree"><ul class="dynatree-container">
  <li id="node~Region~UK-NW">
    <span class="dynatree-node">
      <span class="dynatree-checkbox">&nbsp;</span>
      <a class="dynatree-title">North West (England)</a>
    </span>
  </li>
</ul></div>
"""


class _FakeBridge:
    """Records every call and serves a queued HTML for each rendered_html.

    `html_responses` is a list — popped left-to-right; the last value is
    repeated if the driver asks again. That mirrors the live behaviour:
    after Search settles, rendered_html returns the same DOM until the next
    interaction.
    """

    def __init__(self, html_responses: list[str], errors_on: tuple[str, ...] = ()):
        self.html_responses = list(html_responses)
        self.errors_on = errors_on
        self.clicks: list[str] = []
        self.fills: list[tuple[str, str]] = []
        self.navigates: list[str] = []
        self.rendered_html_waits: list[str] = []
        self.select_options: list[tuple[str, str | None, str | None]] = []

    async def navigate(self, _slug, url):
        self.navigates.append(url)
        return {"current_url": url}

    async def click(self, _slug, selector):
        for needle in self.errors_on:
            if needle.lower() in selector.lower():
                raise BridgeError(f"click failed on {selector}")
        self.clicks.append(selector)
        return {"ok": True}

    async def fill(self, _slug, selector, value):
        self.fills.append((selector, value))
        return {"ok": True}

    async def select_option(self, _slug, selector, label=None, value=None, index=None):
        self.select_options.append((selector, label, value))
        return {"ok": True}

    async def rendered_html(self, _slug, *, wait_for_selector=None, **_kw):
        self.rendered_html_waits.append(wait_for_selector or "")
        if not self.html_responses:
            return RenderedPage(html="", wait_satisfied=False, current_url=None)
        if len(self.html_responses) == 1:
            html = self.html_responses[0]
        else:
            html = self.html_responses.pop(0)
        return RenderedPage(html=html, wait_satisfied=True, current_url=None)


@pytest.mark.asyncio
async def test_driver_ticks_one_matching_category():
    bridge = _FakeBridge(
        html_responses=["<div id='DivTree'></div>", _POPULATED_HTML]
    )
    outcome = await _drive_dynatree_popup(
        bridge,
        open_trigger_selector=".trigger",
        items=["45000000"],
        matcher="code",
        popup_kind="category",
    )
    assert outcome.applied == 1
    assert outcome.requested == 1
    assert outcome.not_found == []
    # The driver typed the CODE into the search box (search-by-code rule).
    pwd_fills = [v for s, v in bridge.fills if "TxtFilterNodes" in s or "search" in s.lower()]
    assert "45000000" in pwd_fills
    # Clear-then-fill: an EMPTY value was pushed first.
    assert "" in pwd_fills
    # And the specific node's checkbox span was clicked.
    assert any(
        'li[id="node~CPV~45000000-7"] span.dynatree-checkbox' in c
        for c in bridge.clicks
    )
    # Apply (BtnSelect) was clicked once.
    assert sum(1 for c in bridge.clicks if "BtnSelect" in c) == 1


@pytest.mark.asyncio
async def test_driver_records_not_found_when_search_returns_zero():
    bridge = _FakeBridge(
        html_responses=[
            "<div id='DivTree'></div>",  # popup-open wait
            _NO_RESULTS_HTML,            # search settled with marker visible
        ]
    )
    outcome = await _drive_dynatree_popup(
        bridge,
        open_trigger_selector=".trigger",
        items=["99999999"],
        matcher="code",
        popup_kind="category",
    )
    assert outcome.applied == 0
    assert outcome.not_found == ["99999999"]
    # Even with nothing matched the driver still clicks Apply once to close
    # the popup cleanly.
    assert sum(1 for c in bridge.clicks if "BtnSelect" in c) == 1


@pytest.mark.asyncio
async def test_driver_continues_through_mixed_match_and_miss():
    bridge = _FakeBridge(
        html_responses=[
            "<div id='DivTree'></div>",  # popup open
            _POPULATED_HTML,             # search 45000000 (matched)
            _NO_RESULTS_HTML,            # search 99999999 (not found)
            _POPULATED_HTML,             # search 72000000 (matched)
        ]
    )
    outcome = await _drive_dynatree_popup(
        bridge,
        open_trigger_selector=".trigger",
        items=["45000000", "99999999", "72000000"],
        matcher="code",
        popup_kind="category",
    )
    assert outcome.requested == 3
    assert outcome.applied == 2
    assert outcome.not_found == ["99999999"]
    # Apply still fires only ONCE at the end (not once per item).
    assert sum(1 for c in bridge.clicks if "BtnSelect" in c) == 1
    # Both matched node checkboxes were clicked, in order.
    tick_clicks = [c for c in bridge.clicks if "dynatree-checkbox" in c]
    assert tick_clicks == [
        'li[id="node~CPV~45000000-7"] span.dynatree-checkbox',
        'li[id="node~CPV~72000000-5"] span.dynatree-checkbox',
    ]


@pytest.mark.asyncio
async def test_driver_reports_popup_open_failure():
    bridge = _FakeBridge(html_responses=[], errors_on=(".trigger",))
    outcome = await _drive_dynatree_popup(
        bridge,
        open_trigger_selector=".trigger",
        items=["45000000"],
        matcher="code",
        popup_kind="category",
    )
    assert outcome.popup_opened is False
    assert outcome.error is not None
    assert outcome.applied == 0
    # Nothing typed when the popup never opened.
    assert bridge.fills == []


@pytest.mark.asyncio
async def test_driver_typed_the_code_not_a_free_word():
    """The spec is explicit: search by CPV code, not by descriptive word.
    Wrap that as a property — the search input only ever sees codes from
    the items list (and the clear-then-fill empty string)."""
    bridge = _FakeBridge(
        html_responses=["<div id='DivTree'></div>", _POPULATED_HTML]
    )
    await _drive_dynatree_popup(
        bridge,
        open_trigger_selector=".trigger",
        items=["72000000"],
        matcher="code",
        popup_kind="category",
    )
    typed = [v for s, v in bridge.fills if "TxtFilterNodes" in s]
    assert "construction" not in [t.lower() for t in typed]
    assert "72000000" in typed


# ---------------------------------------------------------------------------
# Region popup — same component, different matcher.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_region_driver_matches_case_insensitively():
    bridge = _FakeBridge(
        html_responses=["<div id='DivTree'></div>", _REGION_HTML]
    )
    outcome = await _drive_dynatree_popup(
        bridge,
        open_trigger_selector=".trigger",
        items=["north west"],
        matcher="region",
        popup_kind="region",
    )
    assert outcome.applied == 1
    assert outcome.not_found == []
    assert any(
        'li[id="node~Region~UK-NW"]' in c for c in bridge.clicks
    )


# ---------------------------------------------------------------------------
# _apply_filters_from_profile end-to-end — categories + regions counters land
# on the DiscoveryRunResult.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_filters_from_profile_populates_run_summary():
    bridge = _FakeBridge(
        html_responses=[
            # navigate gets called first; then categories popup open + search.
            "<div id='DivTree'></div>",   # category popup open
            _POPULATED_HTML,              # search 45000000
            # regions popup open + search.
            "<div id='DivTree'></div>",   # region popup open
            _REGION_HTML,                 # search North West
            # Listing render wait.
            "<table class='opportunities'></table>",
        ]
    )
    config = ProactisFilterConfig(
        keywords="cyber",
        categories=["45000000"],
        regions=["North West"],
        include_closed=False,
    )
    result = DiscoveryRunResult(status="ok")
    await _apply_filters_from_profile(bridge, config, result)
    assert result.categories_requested == 1
    assert result.categories_applied == 1
    assert result.categories_not_found == []
    assert result.regions_requested == 1
    assert result.regions_applied == 1
    assert result.regions_not_found == []


@pytest.mark.asyncio
async def test_apply_filters_from_profile_records_misses_without_aborting():
    bridge = _FakeBridge(
        html_responses=[
            "<div id='DivTree'></div>",  # category popup open
            _NO_RESULTS_HTML,            # search 99999999 — no match
            "<div id='DivTree'></div>",  # region popup open
            _NO_RESULTS_HTML,            # search "Atlantis" — no match
            "<table class='opportunities'></table>",
        ]
    )
    config = ProactisFilterConfig(
        keywords="",
        categories=["99999999"],
        regions=["Atlantis"],
        include_closed=False,
    )
    result = DiscoveryRunResult(status="ok")
    await _apply_filters_from_profile(bridge, config, result)
    assert result.categories_applied == 0
    assert result.categories_not_found == ["99999999"]
    assert result.regions_applied == 0
    assert result.regions_not_found == ["Atlantis"]


# ---------------------------------------------------------------------------
# Portal scope — a SINGLE-value dropdown (confirmed by the 2026-06-10 live
# capture: `FilterResultItems.PortalWithAllOptionFilter`, default "All"),
# looped one search per configured portal. NOT a Dynatree popup.
# ---------------------------------------------------------------------------

# Real option labels from the live capture ("London Tenders", "South East
# Business Portal", "EastMidsTenders" verbatim); GUID-shaped values are
# synthetic stand-ins (the capture relayed labels, not values).
_PORTAL_OPTION_ROWS = [
    {"label": "All", "value": ""},
    {"label": "London Tenders", "value": "11111111-aaaa-4bbb-8ccc-000000000001"},
    {"label": "South East Business Portal", "value": "11111111-aaaa-4bbb-8ccc-000000000002"},
    {"label": "EastMidsTenders", "value": "11111111-aaaa-4bbb-8ccc-000000000003"},
    {"label": "The Chest - North West", "value": "11111111-aaaa-4bbb-8ccc-000000000004"},
]


class _DropdownBridge(_FakeBridge):
    """_FakeBridge plus the `evaluate` surface the portal resolver uses."""

    def __init__(self, *args, option_rows=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.option_rows = (
            option_rows if option_rows is not None else list(_PORTAL_OPTION_ROWS)
        )
        self.evaluations: list[str] = []

    async def evaluate(self, _slug, script):
        self.evaluations.append(script)
        if "PortalWithAllOption" in script:
            return self.option_rows
        return None


@pytest.mark.asyncio
async def test_resolver_maps_names_to_option_values_with_tolerance():
    from tender_agent.services.discovery.proactis_discovery import (
        _resolve_portal_targets,
    )

    bridge = _DropdownBridge(html_responses=[])
    config = ProactisFilterConfig(
        portals=["EastMidsTenders", "the chest", "Atlantis"]
    )
    result = DiscoveryRunResult(status="ok")
    targets = await _resolve_portal_targets(bridge, config, result)

    assert result.portals_requested == 3
    # Exact (case differences allowed) + prefix tolerance both resolve to
    # the REAL labels + their GUID values; the unknown name is loud.
    assert ("EastMidsTenders", "11111111-aaaa-4bbb-8ccc-000000000003") in targets
    assert (
        "The Chest - North West",
        "11111111-aaaa-4bbb-8ccc-000000000004",
    ) in targets
    assert result.portals_not_found == ["Atlantis"]


@pytest.mark.asyncio
async def test_resolver_never_fuzzy_matches_the_all_option():
    """A configured name must never silently widen scope to "All" — only an
    EXACT "All" can select it."""
    from tender_agent.services.discovery.proactis_discovery import (
        _match_portal_option,
    )

    options = [(r["label"], r["value"]) for r in _PORTAL_OPTION_ROWS]
    assert _match_portal_option(options, "Al") is None
    assert _match_portal_option(options, "all") == ("All", "")


@pytest.mark.asyncio
async def test_apply_portal_selection_selects_by_value_and_updates():
    from tender_agent.services.discovery.proactis_discovery import (
        _apply_portal_selection,
    )

    bridge = _DropdownBridge(
        html_responses=["<table class='opportunities'></table>"]
    )
    ok = await _apply_portal_selection(
        bridge, "London Tenders", "11111111-aaaa-4bbb-8ccc-000000000001"
    )
    assert ok is True
    # Selected by GUID VALUE on the real select, then Update clicked.
    assert any(
        "PortalWithAllOption" in sel
        and value == "11111111-aaaa-4bbb-8ccc-000000000001"
        for sel, _label, value in bridge.select_options
    )
    assert any("Update" in c for c in bridge.clicks)


@pytest.mark.asyncio
async def test_resolver_falls_back_to_labels_when_options_unreadable():
    """HTTP bridge / older bridges without `evaluate`: every configured name
    is deferred to exact-label selection so the loop still runs."""
    from tender_agent.services.discovery.proactis_discovery import (
        _resolve_portal_targets,
    )

    bridge = _FakeBridge(html_responses=[])  # no evaluate attribute
    config = ProactisFilterConfig(portals=["London Tenders"])
    result = DiscoveryRunResult(status="ok")
    targets = await _resolve_portal_targets(bridge, config, result)
    assert targets == [("London Tenders", None)]
    assert result.portals_not_found == []


@pytest.mark.asyncio
async def test_empty_portals_leaves_the_control_alone():
    """No portals configured (the default) → the select is never touched by
    the filter step, so the proven first-run behaviour is unchanged."""
    bridge = _FakeBridge(
        html_responses=["<table class='opportunities'></table>"]
    )
    config = ProactisFilterConfig()
    result = DiscoveryRunResult(status="ok")
    await _apply_filters_from_profile(bridge, config, result)
    assert result.portals_requested == 0
    # Only the page-level Update click happens — no popup trigger, no node
    # tick, no popup apply button, no portal select.
    assert all("Update" in c for c in bridge.clicks)
    assert not any("portal" in c.lower() for c in bridge.clicks)
    assert not any(
        "PortalWithAllOption" in sel for sel, _l, _v in bridge.select_options
    )


# ---------------------------------------------------------------------------
# CPV prefix expansion — the 2026-06-10 capture showed the live tree titles
# nodes with FULL codes ("45000000-7 - Construction work"), so the driver
# searches and matches with the expanded 8-digit form.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_category_prefix_expands_to_full_cpv_code():
    """The operator's profile stores "45"; the driver must search and tick
    via "45000000" — applied > 0 is the whole point of the fix."""
    bridge = _FakeBridge(
        html_responses=["<div id='DivTree'></div>", _POPULATED_HTML]
    )
    outcome = await _drive_dynatree_popup(
        bridge,
        open_trigger_selector=".trigger",
        items=["45"],
        matcher="code",
        popup_kind="category",
    )
    assert outcome.applied == 1
    assert outcome.not_found == []
    typed = [v for s, v in bridge.fills if "TxtFilterNodes" in s and v]
    assert typed == ["45000000"]  # expanded form, not the bare prefix
    assert any(
        'li[id="node~CPV~45000000-7"] span.dynatree-checkbox' in c
        for c in bridge.clicks
    )


@pytest.mark.asyncio
async def test_category_falls_back_to_raw_term_when_expanded_misses():
    """Belt-and-braces: a tenant tree that doesn't match the 8-digit form
    gets a second search with the raw configured text before we give up."""
    bridge = _FakeBridge(
        html_responses=[
            "<div id='DivTree'></div>",  # popup open
            _NO_RESULTS_HTML,            # search "45000000" — no results
            _POPULATED_HTML,             # search "45" — populated
        ]
    )
    outcome = await _drive_dynatree_popup(
        bridge,
        open_trigger_selector=".trigger",
        items=["45"],
        matcher="code",
        popup_kind="category",
    )
    assert outcome.applied == 1
    typed = [v for s, v in bridge.fills if "TxtFilterNodes" in s and v]
    assert typed == ["45000000", "45"]


@pytest.mark.asyncio
async def test_category_not_found_when_both_terms_miss():
    """Safety preserved: a code with genuinely no CPV match is logged and
    skipped — never aborts the run, Apply still closes the popup."""
    bridge = _FakeBridge(
        html_responses=[
            "<div id='DivTree'></div>",  # popup open
            _NO_RESULTS_HTML,            # search "99000000"
            _NO_RESULTS_HTML,            # search "99"
        ]
    )
    outcome = await _drive_dynatree_popup(
        bridge,
        open_trigger_selector=".trigger",
        items=["99"],
        matcher="code",
        popup_kind="category",
    )
    assert outcome.applied == 0
    assert outcome.not_found == ["99"]
    assert sum(1 for c in bridge.clicks if "BtnSelect" in c) == 1


@pytest.mark.asyncio
async def test_apply_filters_opens_the_cpv_trigger_not_unspsc():
    """The category step must click the "Add CPV categories" anchor — the
    old bare "Add" selector opened the UNSPSC tree where 45 is printing
    equipment, which is exactly the live failure this fixes."""
    bridge = _FakeBridge(
        html_responses=[
            "<div id='DivTree'></div>",
            _POPULATED_HTML,
            "<table class='opportunities'></table>",
        ]
    )
    config = ProactisFilterConfig(categories=["45"])
    result = DiscoveryRunResult(status="ok")
    await _apply_filters_from_profile(bridge, config, result)
    assert result.categories_applied == 1
    trigger_clicks = [c for c in bridge.clicks if "categor" in c.lower()]
    assert any("Add CPV categories" in c for c in trigger_clicks)
    assert not any("UNSPSC" in c for c in bridge.clicks)
