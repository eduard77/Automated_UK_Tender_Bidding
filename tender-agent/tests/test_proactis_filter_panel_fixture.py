"""Proactis filter-panel selectors vs the captured panel markup — offline.

The 2026-06-10 live filter-diagnostic resolved two failures:
  1. the bare "Add" category trigger opened the UNSPSC tree (FIRST of five
     `a.CategoryFilter` anchors in document order) — CPV codes matched the
     wrong taxonomy, so every run was effectively un-CPV-filtered;
  2. there is no "Add portal" control — portal scope is a single-value
     <select name="FilterResultItems.PortalWithAllOptionFilter">.

These tests pin the FIXED selectors against the fixture reconstruction of
that panel (tests/fixtures/proactis_filter_panel.html) so a selector/markup
drift fails here, not 30s into a cloud run. The selector evaluator is shared
with the login-page contract tests.
"""
from __future__ import annotations

import re

from tender_agent.services.portals.adapters.proactis import PROACTIS_SELECTORS
from tests.conftest import load_text_fixture
from tests.test_proactis_login_fixture import _parse_elements, _selector_hits

FIXTURE = "proactis_filter_panel.html"

#: The selector that produced the live failure (kept verbatim as the
#: regression artefact — see test_old_bare_add_selector_hit_unspsc_first).
_OLD_CATEGORY_SELECTOR = "button:has-text('Add'), a:has-text('Add')"


def test_category_trigger_hits_only_the_cpv_anchor():
    elements = _parse_elements(load_text_fixture(FIXTURE))
    hits = _selector_hits(
        elements, PROACTIS_SELECTORS["opp_add_category_button"]
    )
    assert hits, "CPV trigger selector matched nothing on the captured panel"
    # EVERY hit (whatever the DOM order Playwright picks first) is the CPV
    # anchor — never UNSPSC / eClass / ProClass / Proc HE.
    for hit in hits:
        assert "Add CPV categories" in hit["text"]
        assert "UNSPSC" not in hit["text"]
    assert "CategoryFilter" in (hits[0]["attrs"].get("class") or "")


def test_old_bare_add_selector_hit_unspsc_first():
    """Regression documentation: the OLD selector's first document-order
    match on this panel is the UNSPSC anchor — exactly the live failure
    (popup heading "UNSPSC category selection", code 45 = printing)."""
    elements = _parse_elements(load_text_fixture(FIXTURE))
    hits = _selector_hits(elements, _OLD_CATEGORY_SELECTOR)
    assert hits
    assert "Add UNSPSC categories" in hits[0]["text"]


def test_portal_select_selector_matches_the_real_dropdown():
    elements = _parse_elements(load_text_fixture(FIXTURE))
    hits = _selector_hits(elements, PROACTIS_SELECTORS["opp_portal_select"])
    assert hits, "portal select selector matched nothing on the captured panel"
    select = hits[0]
    assert select["tag"] == "select"
    assert (
        select["attrs"].get("name")
        == "FilterResultItems.PortalWithAllOptionFilter"
    )
    # Single-value control: no `multiple` attribute on the captured markup —
    # which is why discovery loops one search per configured portal.
    assert "multiple" not in select["attrs"]


def test_fixture_options_carry_the_confirmed_labels():
    """The three labels the capture confirmed verbatim, available for the
    name→option matcher to resolve against."""
    html = load_text_fixture(FIXTURE)
    labels = re.findall(r"<option[^>]*>([^<]+)</option>", html)
    labels = [label.strip() for label in labels]
    assert "All" in labels
    assert "London Tenders" in labels
    assert "South East Business Portal" in labels
    assert "EastMidsTenders" in labels


def test_region_trigger_still_matches_unchanged():
    """The region control was never broken — make sure the panel fixture
    keeps proving the existing selector."""
    elements = _parse_elements(load_text_fixture(FIXTURE))
    hits = _selector_hits(
        elements, PROACTIS_SELECTORS["opp_add_region_button"]
    )
    assert hits and "Add new region" in hits[0]["text"]
