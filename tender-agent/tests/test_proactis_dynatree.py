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
    match_node_for_code,
    match_node_for_region,
    no_results_visible,
    node_checkbox_selector,
    normalise_code,
    parse_dynatree_nodes,
)

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
