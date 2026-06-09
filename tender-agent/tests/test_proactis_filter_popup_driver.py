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
