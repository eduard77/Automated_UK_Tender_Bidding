"""Dynatree popup parser + matchers.

Pure logic against captured-style popup HTML — no network, no browser.
Fixtures mirror the structure recorded from the live Proactis category /
region popups (jQuery-UI dialog + Dynatree widget + `node~…` li ids +
`a.dynatree-title` text starting with the CPV code).
"""
from __future__ import annotations

import pytest

from tender_agent.services.discovery.proactis_dynatree import (
    DYNATREE_SELECTORS,
    DynatreeNode,
    expand_cpv_prefix,
    match_node_for_code,
    match_node_for_region,
    no_results_visible,
    node_checkbox_selector,
    normalise_code,
    parse_dynatree_nodes,
    read_search_state,
    scope_to_open_dialog,
    selection_confirmed,
    tree_loading,
)
from tests.conftest import load_text_fixture

# ---------------------------------------------------------------------------
# Fixture HTML — captured-shape Dynatree popups.
# Real popup includes ~hundreds of nodes; the fixtures keep enough structure
# to be representative (multiple matches, mixed code formats, no-result).
# ---------------------------------------------------------------------------

_CATEGORY_POPUP_HTML = """
<div id="dialog-category">
  <input id="TxtFilterNodes" class="CategoryTextBox" name="search"
         value="Enter the search criteria..." />
  <input id="BtnSearchCat" value="Search" type="button" />
  <label><input id="radExact" name="match" value="ExactSearch" type="radio" checked /> Exact</label>
  <label><input id="radFuzzy" name="match" value="FuzzySearch" type="radio" /> Fuzzy</label>
  <div id="DivTree">
    <ul class="dynatree-container">
      <li id="node~CPV~45000000-7">
        <span class="dynatree-node">
          <span class="dynatree-expander">&nbsp;</span>
          <span class="dynatree-checkbox">&nbsp;</span>
          <a class="dynatree-title">45000000-7 - Construction work</a>
        </span>
      </li>
      <li id="node~CPV~45100000-8">
        <span class="dynatree-node">
          <span class="dynatree-expander">&nbsp;</span>
          <span class="dynatree-checkbox">&nbsp;</span>
          <a class="dynatree-title">45100000-8 - Site preparation work</a>
        </span>
      </li>
      <li id="node~CPV~72000000-5">
        <span class="dynatree-node">
          <span class="dynatree-expander">&nbsp;</span>
          <span class="dynatree-checkbox">&nbsp;</span>
          <a class="dynatree-title">72000000-5 - IT services: consulting</a>
        </span>
      </li>
    </ul>
  </div>
  <div id="DivSelected"></div>
  <input id="BtnSelect" value="Select categories" type="button" />
  <a class="cancel" href="#">Cancel</a>
</div>
"""

_NO_RESULTS_POPUP_HTML = """
<div id="dialog-category">
  <input id="TxtFilterNodes" class="CategoryTextBox" />
  <input id="BtnSearchCat" value="Search" type="button" />
  <div id="DivTree">
    <ul class="dynatree-container"></ul>
  </div>
  <div id="divNoSearchResults">No matching categories found.</div>
  <input id="BtnSelect" value="Select categories" type="button" />
</div>
"""

_REGION_POPUP_HTML = """
<div id="dialog-region">
  <input id="TxtFilterNodes" class="CategoryTextBox" />
  <input id="BtnSearchCat" value="Search" type="button" />
  <div id="DivTree">
    <ul class="dynatree-container">
      <li id="node~Region~UK-NW">
        <span class="dynatree-node">
          <span class="dynatree-checkbox">&nbsp;</span>
          <a class="dynatree-title">North West (England)</a>
        </span>
      </li>
      <li id="node~Region~UK-LON">
        <span class="dynatree-node">
          <span class="dynatree-checkbox">&nbsp;</span>
          <a class="dynatree-title">London</a>
        </span>
      </li>
      <li id="node~Region~UK">
        <span class="dynatree-node">
          <span class="dynatree-checkbox">&nbsp;</span>
          <a class="dynatree-title">UK - United Kingdom</a>
        </span>
      </li>
    </ul>
  </div>
  <div id="DivSelected"></div>
  <input id="BtnSelect" value="Select regions" type="button" />
</div>
"""


# ---------------------------------------------------------------------------
# parse_dynatree_nodes
# ---------------------------------------------------------------------------


def test_parse_extracts_all_three_category_nodes():
    nodes = parse_dynatree_nodes(_CATEGORY_POPUP_HTML)
    assert [n.node_id for n in nodes] == [
        "node~CPV~45000000-7",
        "node~CPV~45100000-8",
        "node~CPV~72000000-5",
    ]


def test_parse_extracts_leading_code_token_ignoring_checkdigit():
    nodes = parse_dynatree_nodes(_CATEGORY_POPUP_HTML)
    assert nodes[0].code_token == "45000000"
    assert nodes[2].code_token == "72000000"


def test_parse_strips_html_tags_in_title():
    html = (
        '<li id="node~A"><span class="dynatree-node">'
        '<a class="dynatree-title"><strong>45000000-7</strong> '
        '<em>Construction</em></a></span></li>'
    )
    nodes = parse_dynatree_nodes(html)
    assert nodes[0].title_text == "45000000-7 Construction"
    assert nodes[0].code_token == "45000000"


def test_parse_returns_empty_when_no_nodes():
    nodes = parse_dynatree_nodes(_NO_RESULTS_POPUP_HTML)
    assert nodes == []


def test_parse_region_nodes_have_no_code_token():
    nodes = parse_dynatree_nodes(_REGION_POPUP_HTML)
    assert all(n.code_token is None for n in nodes)
    assert nodes[0].title_text == "North West (England)"


# ---------------------------------------------------------------------------
# no_results_visible
# ---------------------------------------------------------------------------


def test_no_results_visible_picks_up_marker_div():
    assert no_results_visible(_NO_RESULTS_POPUP_HTML) is True


def test_no_results_visible_false_on_populated_popup():
    assert no_results_visible(_CATEGORY_POPUP_HTML) is False


# ---------------------------------------------------------------------------
# normalise_code — used both by typing the search term and by matching
# titles. Spec rule: `45000000` and `45000000-7` compare equal.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("45000000", "45000000"),
        ("45000000-7", "45000000"),
        ("  45000000  ", "45000000"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalise_code_strips_checkdigit_and_whitespace(raw, expected):
    assert normalise_code(raw) == expected


# ---------------------------------------------------------------------------
# match_node_for_code — search-by-CODE per the captured behaviour.
# ---------------------------------------------------------------------------


def test_match_node_by_exact_code():
    nodes = parse_dynatree_nodes(_CATEGORY_POPUP_HTML)
    match = match_node_for_code(nodes, "45000000")
    assert match is not None
    assert match.node_id == "node~CPV~45000000-7"


def test_match_node_suffix_insensitive_against_title_with_checkdigit():
    """The brief is explicit: '45000000' must match a title '45000000-7 - ...'.
    We compare on the 8-digit token extracted at parse time."""
    nodes = parse_dynatree_nodes(_CATEGORY_POPUP_HTML)
    match = match_node_for_code(nodes, "45000000")
    assert match is not None
    assert match.title_text.startswith("45000000-7")


def test_match_node_for_code_returns_none_when_absent():
    nodes = parse_dynatree_nodes(_CATEGORY_POPUP_HTML)
    assert match_node_for_code(nodes, "99999999") is None


def test_match_node_for_code_returns_none_against_region_popup():
    """Region nodes have no leading code token; a code lookup must NOT
    spuriously latch onto a region title."""
    nodes = parse_dynatree_nodes(_REGION_POPUP_HTML)
    assert match_node_for_code(nodes, "45000000") is None


def test_match_node_for_code_handles_caller_passing_checkdigit():
    nodes = parse_dynatree_nodes(_CATEGORY_POPUP_HTML)
    # `45000000-7` from a profile should still match the same node as `45000000`.
    match = match_node_for_code(nodes, "45000000-7")
    assert match is not None
    assert match.code_token == "45000000"


# ---------------------------------------------------------------------------
# match_node_for_region — case-insensitive title match.
# ---------------------------------------------------------------------------


def test_match_region_case_insensitive_prefix():
    nodes = parse_dynatree_nodes(_REGION_POPUP_HTML)
    match = match_node_for_region(nodes, "north west")
    assert match is not None
    assert match.node_id == "node~Region~UK-NW"


def test_match_region_substring_inside_title():
    """'England' isn't the title of any standalone node, but if Proactis ever
    renders 'UK > North West (England)' the loose substring path should still
    latch onto it. Belt-and-braces tested with the existing fixture."""
    nodes = parse_dynatree_nodes(_REGION_POPUP_HTML)
    # "england" is inside "North West (England)" — should match the first
    # node carrying it.
    match = match_node_for_region(nodes, "England")
    assert match is not None
    assert match.node_id == "node~Region~UK-NW"


def test_match_region_returns_none_for_unknown_region():
    nodes = parse_dynatree_nodes(_REGION_POPUP_HTML)
    assert match_node_for_region(nodes, "Faroe Islands") is None


def test_match_region_returns_none_for_empty_target():
    nodes = parse_dynatree_nodes(_REGION_POPUP_HTML)
    assert match_node_for_region(nodes, "") is None
    assert match_node_for_region(nodes, "   ") is None


# ---------------------------------------------------------------------------
# node_checkbox_selector — built selector targets THIS node only.
# ---------------------------------------------------------------------------


def test_node_checkbox_selector_is_node_scoped():
    sel = node_checkbox_selector("node~CPV~45000000-7")
    assert sel == 'li[id="node~CPV~45000000-7"] span.dynatree-checkbox'


def test_dynatree_selectors_pin_the_captured_ids():
    """Captured live: search box is `#TxtFilterNodes`, search button
    `#BtnSearchCat`, apply `#BtnSelect`. If Proactis renames these, this
    test will fail loudly so the audit lands in one place."""
    assert "TxtFilterNodes" in DYNATREE_SELECTORS["search_input"]
    assert DYNATREE_SELECTORS["search_button"] == "input#BtnSearchCat"
    assert DYNATREE_SELECTORS["apply_button"] == "input#BtnSelect"
    assert DYNATREE_SELECTORS["exact_match_radio"] == "input#radExact"
    assert "divNoSearchResults" in DYNATREE_SELECTORS["tree_settled_wait"]


# ---------------------------------------------------------------------------
# Demonstrates the brief's key rule: search BY CODE, not by free word.
# The driver in `proactis_discovery._drive_dynatree_popup` types the code
# verbatim into the search input. The parser then matches on the SAME
# normalised code. This test pins the round-trip property.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "operator_code,expected_node_id",
    [
        ("45000000", "node~CPV~45000000-7"),
        ("45000000-7", "node~CPV~45000000-7"),  # also accepted
        ("72000000", "node~CPV~72000000-5"),
    ],
)
def test_round_trip_search_by_code_finds_the_intended_node(
    operator_code, expected_node_id
):
    nodes = parse_dynatree_nodes(_CATEGORY_POPUP_HTML)
    match = match_node_for_code(nodes, operator_code)
    assert match is not None
    assert match.node_id == expected_node_id


# ---------------------------------------------------------------------------
# CPV prefix expansion (2026-06-10 fix): profile prefixes -> the 8-digit form
# the live CPV tree displays and the popup search matches.
# ---------------------------------------------------------------------------


def test_expand_two_digit_prefix_pads_to_division_code():
    assert expand_cpv_prefix("45") == "45000000"
    assert expand_cpv_prefix("09") == "09000000"


def test_expand_intermediate_prefixes():
    assert expand_cpv_prefix("451") == "45100000"
    assert expand_cpv_prefix("4511") == "45110000"


def test_full_codes_pass_through():
    assert expand_cpv_prefix("45000000") == "45000000"
    # Check digit stripped (normalise_code), code otherwise untouched.
    assert expand_cpv_prefix("45000000-7") == "45000000"


def test_non_numeric_labels_pass_through():
    assert expand_cpv_prefix("Construction work") == "Construction work"
    assert expand_cpv_prefix("") == ""
    assert expand_cpv_prefix("  45  ") == "45000000"


# ---------------------------------------------------------------------------
# Detection fix (2026-06-10 follow-up capture): the SEARCH worked but the
# detection misread it. The live popup keeps #divNoSearchResults in the DOM
# permanently (hidden when there are results), popup ids repeat across stale
# dialog instances, and the tree loads asynchronously behind a Loading…
# status node. `read_search_state` is the single scoped + settled classifier
# both the driver and the diagnostic use.
# ---------------------------------------------------------------------------

_LIVE_CPV_POPUP_HTML = load_text_fixture("proactis_cpv_popup_live.html")

_LOADING_POPUP_HTML = """
<div class="ui-dialog ui-widget" style="display: block;">
  <div class="ui-dialog-content">
    <h1>CPV category selection</h1>
    <input id="TxtFilterNodes" type="text" value="45000000" />
    <div id="divNoSearchResults" style="display: none;">Your criteria returned no results</div>
    <div id="DivTree">
      <ul class="dynatree-container">
        <li><span class="dynatree-node dynatree-statusnode-wait">
          <a class="dynatree-title">Loading&#8230;</a>
        </span></li>
      </ul>
    </div>
  </div>
</div>
"""

_EMPTY_RESULT_POPUP_HTML = """
<div class="ui-dialog ui-widget" style="display: block;">
  <div class="ui-dialog-content">
    <h1>CPV category selection</h1>
    <div id="divNoSearchResults">Your criteria returned no results</div>
    <div id="DivTree"><ul class="dynatree-container"></ul></div>
  </div>
</div>
"""

# A STALE (closed) dialog whose own marker is VISIBLE — left behind by a
# previous category-popup instantiation. Its ids duplicate the open dialog's.
_STALE_DIALOG_VISIBLE_MARKER = """
<div class="ui-dialog ui-widget" style="display: none;">
  <div class="ui-dialog-content">
    <h1>UNSPSC category selection</h1>
    <div id="divNoSearchResults">Your criteria returned no results</div>
    <div id="DivTree"><ul class="dynatree-container"></ul></div>
  </div>
</div>
"""

_STALE_DIALOG_WITH_NODES = """
<div class="ui-dialog ui-widget" style="display: none;">
  <div class="ui-dialog-content">
    <h1>UNSPSC category selection</h1>
    <div id="divNoSearchResults" style="display: none;"></div>
    <div id="DivTree"><ul class="dynatree-container">
      <li id="node~UNSPSC~45000000"><span class="dynatree-node">
        <span class="dynatree-checkbox">&nbsp;</span>
        <a class="dynatree-title">45000000 - Printing and Photographic Equipment</a>
      </span></li>
    </ul></div>
  </div>
</div>
"""


def test_live_capture_classifies_as_results_despite_hidden_marker():
    """THE regression: tree filtered to `45000000-7 - Construction work`,
    marker present-but-hidden — must be RESULTS, never a miss."""
    state = read_search_state(_LIVE_CPV_POPUP_HTML)
    assert state.status == "results"
    assert [n.node_id for n in state.nodes] == ["node~CPV~45000000-7"]
    assert state.nodes[0].title_text == "45000000-7 - Construction work"


def test_live_capture_node_matches_every_check_digit_form():
    state = read_search_state(_LIVE_CPV_POPUP_HTML)
    for code in ("45000000", "45000000-7", "45"):
        # "45" is expanded by the driver before matching; simulate that.
        target = expand_cpv_prefix(code)
        match = match_node_for_code(state.nodes, target)
        assert match is not None, code
        assert match.node_id == "node~CPV~45000000-7"


def test_loading_status_node_classifies_as_loading_not_no_match():
    state = read_search_state(_LOADING_POPUP_HTML)
    assert state.status == "loading"
    assert tree_loading(state.scope_html) is True


def test_genuinely_empty_result_classifies_as_no_results():
    state = read_search_state(_EMPTY_RESULT_POPUP_HTML)
    assert state.status == "no_results"


def test_stale_dialog_marker_is_ignored_when_open_dialog_has_results():
    """Duplicate-ID scenario from the brief: the marker is VISIBLE in a
    STALE (hidden) dialog only — the scoped check must read the OPEN
    dialog and report results."""
    page = _STALE_DIALOG_VISIBLE_MARKER + _LIVE_CPV_POPUP_HTML
    state = read_search_state(page)
    assert state.status == "results"
    assert state.nodes[0].node_id == "node~CPV~45000000-7"
    # Order independence: stale dialog after the open one too.
    state = read_search_state(_LIVE_CPV_POPUP_HTML + _STALE_DIALOG_VISIBLE_MARKER)
    assert state.status == "results"


def test_stale_dialog_nodes_are_ignored_when_open_dialog_is_empty():
    """The mirror hazard: leftover NODES in a hidden UNSPSC dialog must not
    fake a result when the open CPV dialog genuinely matched nothing."""
    page = _STALE_DIALOG_WITH_NODES + _EMPTY_RESULT_POPUP_HTML
    state = read_search_state(page)
    assert state.status == "no_results"
    assert state.nodes == []


def test_unwrapped_popup_html_still_classifies():
    """Older/test markup without a ui-dialog wrapper passes through the
    scoper unchanged — detection stays backward-compatible."""
    html = (
        '<div id="DivTree"><ul class="dynatree-container">'
        '<li id="node~CPV~71000000-8"><span class="dynatree-node">'
        '<a class="dynatree-title">71000000-8 - Architectural services</a>'
        "</span></li></ul></div>"
    )
    assert scope_to_open_dialog(html) == html
    assert read_search_state(html).status == "results"


def test_all_dialogs_closed_classifies_as_pending():
    state = read_search_state(_STALE_DIALOG_VISIBLE_MARKER)
    assert state.status == "pending"


def test_later_open_non_popup_dialog_does_not_shadow_the_popup():
    """A session-timeout warning / validation alert appended (and open)
    AFTER the popup must not win last-open-wins — the popup-marked chunk
    is the one read."""
    timeout_dialog = (
        '<div class="ui-dialog ui-widget" style="display: block;">'
        "<p>Your session will expire soon</p></div>"
    )
    state = read_search_state(_LIVE_CPV_POPUP_HTML + timeout_dialog)
    assert state.status == "results"
    assert state.nodes[0].node_id == "node~CPV~45000000-7"
    # And in front of the popup too.
    state = read_search_state(timeout_dialog + _LIVE_CPV_POPUP_HTML)
    assert state.status == "results"


def test_unwrapped_popup_survives_a_hidden_dialog_elsewhere_on_the_page():
    """Safety net for older tenant variants: the popup renders WITHOUT a
    ui-dialog wrapper while some unrelated hidden dialog sits appended at
    the end of body — the pre-wrapper page content must still be read (not
    classified pending forever)."""
    unwrapped = (
        '<div id="DivTree"><ul class="dynatree-container">'
        '<li id="node~CPV~45000000-7"><span class="dynatree-node">'
        '<span class="dynatree-checkbox">&nbsp;</span>'
        '<a class="dynatree-title">45000000-7 - Construction work</a>'
        "</span></li></ul></div>"
    )
    state = read_search_state(unwrapped + _STALE_DIALOG_VISIBLE_MARKER)
    assert state.status == "results"
    assert state.nodes[0].node_id == "node~CPV~45000000-7"


def test_no_results_visible_requires_displayed_marker():
    hidden = '<div id="divNoSearchResults" style="display: none;">none</div>'
    shown = '<div id="divNoSearchResults">none</div>'
    assert no_results_visible(hidden) is False
    assert no_results_visible(shown) is True
    assert no_results_visible(hidden + shown) is True


def test_match_falls_back_to_title_prefix_when_token_missing():
    """Fix 1 belt-and-braces: a title whose leading token the extractor
    didn't recognise still matches by check-digit-tolerant prefix."""
    node = DynatreeNode(
        node_id="node~CPV~45000000-7",
        title_text="45000000-7 - Construction work",
        code_token=None,  # token extraction failed (formatting drift)
    )
    match = match_node_for_code([node], "45000000")
    assert match is node
    # A different code must NOT prefix-match.
    assert match_node_for_code([node], "45100000") is None


def test_selection_confirmed_reads_the_open_dialogs_selected_panel():
    ticked = _LIVE_CPV_POPUP_HTML.replace(
        '<div id="DivSelected"></div>',
        '<div id="DivSelected"><span>45000000-7 - Construction work</span></div>',
    )
    assert selection_confirmed(ticked, "45000000") is True
    # Panel present but the selection is missing -> explicit False.
    assert selection_confirmed(_LIVE_CPV_POPUP_HTML, "45000000") is False
    # Panel absent -> None (cannot verify, never treated as failure).
    assert selection_confirmed("<div id='DivTree'></div>", "45000000") is None


def test_node_checkbox_selector_scoped_variant():
    bare = node_checkbox_selector("node~CPV~45000000-7")
    scoped = node_checkbox_selector("node~CPV~45000000-7", scoped=True)
    assert bare == 'li[id="node~CPV~45000000-7"] span.dynatree-checkbox'
    assert scoped == f".ui-dialog:visible {bare}"
