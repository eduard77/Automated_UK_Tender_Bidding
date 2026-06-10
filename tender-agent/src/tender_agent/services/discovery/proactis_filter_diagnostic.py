"""Proactis FILTER-application diagnostic — capture WHAT the browser sees on
the Find Opportunities filter panel, so a real selector/format fix can be
made from the actual markup instead of guessed.

Built for (and proven by) the 2026-06-10 live capture, which resolved two
post-login failures:

  1. Category popup: the bare "Add" trigger opened the UNSPSC tree (where
     45 = printing/photographic, not construction) — the panel has FIVE
     `a.CategoryFilter` triggers and the flow now opens "Add CPV
     categories" specifically. The diagnostic keeps probing one code + one
     word so a future taxonomy/format drift is visible immediately.
  2. Portals: there is NO popup — scope is a plain single-value <select>
     (`FilterResultItems.PortalWithAllOptionFilter`, "All" default). The
     diagnostic probes the select and lists its REAL option labels so the
     configured portal names can be checked against them.

Hard rule, same as the login diagnostic: NO secrets. Page text comes from
`inner_text`, so credential strings (form input VALUES) physically can't
leak through it; we never read cookies.

This module deliberately does not import `proactis_discovery` (which
imports the login diagnostic the same way) — the bridge slug is passed
in by callers.
"""
from __future__ import annotations

import contextlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any

import structlog

from tender_agent.services.bridge_client import BridgeClient, BridgeError
from tender_agent.services.discovery.proactis_dynatree import DYNATREE_SELECTORS
from tender_agent.services.discovery.proactis_login import (
    login_with_credentials,
)
from tender_agent.services.portals.adapters.proactis import (
    PORTAL_OPTIONS_JS,
    PROACTIS_SELECTORS,
    PROACTIS_URLS,
)

logger = structlog.get_logger(__name__)

#: Page-text slice size. Big enough to read the filter-panel labels + the
#: popup's tree contents without flooding one log event.
TEXT_EXCERPT_CHARS = 2000

#: HTML slice size — kept larger than the text slice because we deliberately
#: want enough markup to see node IDs, data attributes, and the surrounding
#: control labels. Capped to keep the JSON response reviewable.
HTML_EXCERPT_CHARS = 4000

#: Example category-popup probes. ONE numeric code and ONE descriptive word.
#: The code probe uses the 8-digit form the discovery flow now searches with
#: (expand_cpv_prefix("45") — the 2026-06-10 capture showed tree nodes title
#: full codes), so the diagnostic exercises exactly what the run does.
CATEGORY_PROBE_CODE = "45000000"
CATEGORY_PROBE_WORD = "construction"


def _collapse_text(text: str, limit: int = TEXT_EXCERPT_CHARS) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


def _slice_html(html: str, limit: int = HTML_EXCERPT_CHARS) -> str:
    """A HTML slice that preserves tags + attributes (the whole point of this
    diagnostic) but caps length to keep responses reviewable."""
    return (html or "")[:limit]


@dataclass
class SearchProbe:
    """One search attempt against the category Dynatree popup.

    `nodes_excerpt` is a TAG-PRESERVING slice of the tree container so the
    operator can see the real li/span/text/attribute shapes."""

    term: str
    matched: bool  # True when the no-results marker is NOT visible
    no_results_marker_visible: bool
    nodes_excerpt: str = ""
    error: str | None = None


@dataclass
class ControlMarkup:
    """One candidate filter-panel control surfaced by a coarse JS scan.

    The scan lists EVERY button/anchor that sits inside the filter form, so
    the operator can see the real text + tag + classes + id of the Portals
    open-trigger (and every other one) instead of us guessing labels."""

    tag: str
    text: str
    id: str | None = None
    classes: str | None = None
    name: str | None = None


@dataclass
class CategoryPopupSnapshot:
    opened: bool
    open_trigger_selector: str
    search_input_present: bool
    search_input_excerpt: str = ""
    probes: list[SearchProbe] = field(default_factory=list)
    screenshot_path: str | None = None
    error: str | None = None


@dataclass
class PortalControlSnapshot:
    """What the filter panel ACTUALLY exposes for portal scoping.

    The 2026-06-10 capture proved portal scope is a plain single-value
    <select> (`FilterResultItems.PortalWithAllOptionFilter`), not a popup.
    This snapshot probes that select and lists its real option labels, so
    PROACTIS_DISCOVERY_PORTALS entries can be checked against the exact
    names the dropdown carries. The control scan + panel HTML stay for the
    next time a control moves."""

    portal_select_selector: str
    portal_select_present: bool
    portal_options: list[str] = field(default_factory=list)
    panel_html_excerpt: str = ""
    panel_text_excerpt: str = ""
    panel_controls: list[ControlMarkup] = field(default_factory=list)
    screenshot_path: str | None = None
    error: str | None = None


@dataclass
class FilterDiagnostic:
    """Non-secret snapshot of the filter panel + the category popup."""

    logged_in: bool
    outcome: str
    detail: str | None
    current_url: str | None
    page_title: str | None
    category_popup: CategoryPopupSnapshot | None = None
    portal_control: PortalControlSnapshot | None = None
    capture_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# JS used to enumerate every clickable control inside the filter form.
# Returns plain serialisable rows — no DOM nodes, no cookies, no inputs'
# values. Scoped tightly to avoid surfacing nav/footer controls that would
# pad the response without helping.
_PANEL_CONTROLS_JS = """
() => {
  const out = [];
  const form = document.querySelector(
    "form[action*='Opportunit' i], form#opportunitiesForm, form.opportunities-filter, form"
  );
  const root = form || document.body;
  const nodes = root.querySelectorAll(
    "button, a, input[type='button'], input[type='submit']"
  );
  for (const el of nodes) {
    const text = (el.innerText || el.value || el.textContent || "").trim();
    if (!text) continue;
    out.push({
      tag: (el.tagName || "").toLowerCase(),
      text: text.slice(0, 80),
      id: el.id || null,
      classes: el.className || null,
      name: el.getAttribute("name") || null,
    });
    if (out.length >= 80) break;
  }
  return out;
}
"""

# JS that returns the OUTER HTML of the filter panel area as a single
# string — the operator can paste it back so we see exactly what surrounds
# the (missing) Portals control.
_PANEL_HTML_JS = """
() => {
  const sels = [
    "form[action*='Opportunit' i]",
    "form#opportunitiesForm",
    "form.opportunities-filter",
    ".narrow-results",
    "aside",
  ];
  for (const s of sels) {
    const el = document.querySelector(s);
    if (el) return el.outerHTML;
  }
  return document.body.outerHTML;
}
"""

# JS that returns the OUTER HTML of the dynatree popup container — the
# whole purpose of probe-and-snapshot is to see the real node shape on a
# CPV miss vs hit.
_POPUP_HTML_JS = """
() => {
  const el = document.querySelector(
    "#DivTree, .dynatree-container, .modal-content, .ui-dialog-content"
  );
  return (el && el.outerHTML) || "";
}
"""


async def _evaluate(bridge: BridgeClient, slug: str, script: str) -> Any:
    """Best-effort bridge.evaluate — None when the bridge lacks the method
    (HTTP client / tests) or the JS errors."""
    evaluate = getattr(bridge, "evaluate", None)
    if evaluate is None:
        return None
    try:
        return await evaluate(slug, script)
    except BridgeError:
        return None


async def _capture_category_popup(
    bridge: BridgeClient, slug: str
) -> CategoryPopupSnapshot:
    open_trigger = PROACTIS_SELECTORS["opp_add_category_button"]
    snapshot = CategoryPopupSnapshot(
        opened=False,
        open_trigger_selector=open_trigger,
        search_input_present=False,
    )
    try:
        await bridge.click(slug, open_trigger)
    except BridgeError as exc:
        snapshot.error = f"open trigger failed: {exc}"
        return snapshot
    snapshot.opened = True

    # Confirm the search box appears and capture its real markup.
    try:
        present = await bridge.element_exists(
            slug, DYNATREE_SELECTORS["search_input"]
        )
        snapshot.search_input_present = present
    except BridgeError as exc:
        snapshot.error = f"search input probe failed: {exc}"
        return snapshot

    popup_html = await _evaluate(bridge, slug, _POPUP_HTML_JS)
    if isinstance(popup_html, str) and popup_html:
        snapshot.search_input_excerpt = _slice_html(popup_html)

    # Probe ONE numeric code and ONE descriptive word. If the code returns
    # `no_results_marker` but the word does not, the diagnosis is the
    # bare-prefix format — exactly the gap the task description suspects.
    for term in (CATEGORY_PROBE_CODE, CATEGORY_PROBE_WORD):
        probe = SearchProbe(
            term=term, matched=False, no_results_marker_visible=False
        )
        try:
            await bridge.fill(slug, DYNATREE_SELECTORS["search_input"], "")
            await bridge.fill(slug, DYNATREE_SELECTORS["search_input"], term)
            await bridge.click(slug, DYNATREE_SELECTORS["search_button"])
        except BridgeError as exc:
            probe.error = f"search drive failed: {exc}"
            snapshot.probes.append(probe)
            continue
        # Wait for the tree to settle OR the no-results marker to appear.
        with contextlib.suppress(BridgeError):
            await bridge.rendered_html(
                slug,
                wait_for_selector=DYNATREE_SELECTORS["tree_settled_wait"],
                timeout_ms=4000,
            )
        try:
            probe.no_results_marker_visible = await bridge.element_exists(
                slug, DYNATREE_SELECTORS["no_results_marker"]
            )
        except BridgeError:
            probe.no_results_marker_visible = False
        probe.matched = not probe.no_results_marker_visible
        # The actual tree markup — the whole point of this diagnostic.
        popup_html = await _evaluate(bridge, slug, _POPUP_HTML_JS)
        if isinstance(popup_html, str) and popup_html:
            probe.nodes_excerpt = _slice_html(popup_html)
        snapshot.probes.append(probe)

    try:
        shot = await bridge.screenshot(slug, label=f"{slug}-category-popup")
        snapshot.screenshot_path = shot.get("path")
    except BridgeError:
        snapshot.screenshot_path = None

    # Close the popup so the next snapshot reads a quiet panel.
    with contextlib.suppress(BridgeError):
        await bridge.click(slug, DYNATREE_SELECTORS["cancel_link"])

    return snapshot


async def _capture_portal_control(
    bridge: BridgeClient, slug: str
) -> PortalControlSnapshot:
    select_selector = PROACTIS_SELECTORS["opp_portal_select"]
    snapshot = PortalControlSnapshot(
        portal_select_selector=select_selector,
        portal_select_present=False,
    )
    try:
        snapshot.portal_select_present = await bridge.element_exists(
            slug, select_selector
        )
    except BridgeError as exc:
        snapshot.error = f"portal-select probe failed: {exc}"

    # The dropdown's real option labels — the source of truth the operator
    # checks PROACTIS_DISCOVERY_PORTALS against. Labels only; the GUID values
    # are resolved at run time by the discovery itself.
    option_rows = await _evaluate(bridge, slug, PORTAL_OPTIONS_JS)
    if isinstance(option_rows, list):
        snapshot.portal_options = [
            str(row.get("label") or "")
            for row in option_rows
            if isinstance(row, dict) and (row.get("label") or "").strip()
        ]

    panel_html = await _evaluate(bridge, slug, _PANEL_HTML_JS)
    if isinstance(panel_html, str) and panel_html:
        snapshot.panel_html_excerpt = _slice_html(panel_html)

    try:
        snapshot.panel_text_excerpt = _collapse_text(
            await bridge.page_text(slug)
        )
    except BridgeError:
        snapshot.panel_text_excerpt = ""

    controls = await _evaluate(bridge, slug, _PANEL_CONTROLS_JS)
    if isinstance(controls, list):
        for row in controls:
            if not isinstance(row, dict):
                continue
            snapshot.panel_controls.append(
                ControlMarkup(
                    tag=str(row.get("tag") or ""),
                    text=str(row.get("text") or ""),
                    id=row.get("id"),
                    classes=row.get("classes"),
                    name=row.get("name"),
                )
            )

    try:
        shot = await bridge.screenshot(slug, label=f"{slug}-portal-control")
        snapshot.screenshot_path = shot.get("path")
    except BridgeError:
        snapshot.screenshot_path = None

    return snapshot


async def capture_filter_state(
    bridge: BridgeClient, slug: str
) -> FilterDiagnostic:
    """Navigate to Find Opportunities and snapshot the filter panel +
    category popup. Caller must have already logged in (the discovery's own
    `_login_then_get_status` is the canonical wrapper). Best-effort: any
    single read failing lands in `capture_error`, never raised."""
    current_url: str | None = None
    page_title: str | None = None
    capture_errors: list[str] = []

    try:
        await bridge.navigate(slug, PROACTIS_URLS["opportunities"])
    except BridgeError as exc:
        capture_errors.append(f"navigate opportunities: {exc}")
    try:
        status = await bridge.session_status(slug)
        current_url = status.get("current_url")
    except BridgeError as exc:
        capture_errors.append(f"session_status: {exc}")

    try:
        page_title_eval = await _evaluate(bridge, slug, "() => document.title")
        if isinstance(page_title_eval, str):
            page_title = page_title_eval
    except BridgeError:
        page_title = None

    # Capture the portal control BEFORE opening the category popup — the
    # popup overlays the panel and would mask the real text/markup.
    portal_snapshot: PortalControlSnapshot | None = None
    try:
        portal_snapshot = await _capture_portal_control(bridge, slug)
    except Exception as exc:  # noqa: BLE001 - best-effort
        capture_errors.append(f"portal capture: {exc}")

    category_snapshot: CategoryPopupSnapshot | None = None
    try:
        category_snapshot = await _capture_category_popup(bridge, slug)
    except Exception as exc:  # noqa: BLE001 - best-effort
        capture_errors.append(f"category capture: {exc}")

    diagnostic = FilterDiagnostic(
        logged_in=True,
        outcome="ok",
        detail=None,
        current_url=current_url,
        page_title=page_title,
        category_popup=category_snapshot,
        portal_control=portal_snapshot,
        capture_error="; ".join(capture_errors) or None,
    )
    logger.info(
        "discovery.proactis.filter_diagnostic",
        current_url=current_url,
        category_opened=bool(
            category_snapshot and category_snapshot.opened
        ),
        category_probes_matched=sum(
            1
            for p in (category_snapshot.probes if category_snapshot else [])
            if p.matched
        ),
        portal_select_present=bool(
            portal_snapshot and portal_snapshot.portal_select_present
        ),
    )
    return diagnostic


async def run_filter_diagnostic(
    *,
    credentials: Any,  # services.portals.base.Credentials
    slug: str,
    bridge: BridgeClient | None = None,
) -> dict[str, Any]:
    """One-shot READ-ONLY filter probe for the admin endpoint: open a
    bridge session, log in with the stored credentials, navigate to Find
    Opportunities, capture the filter panel + category popup, close the
    session.

    Read-only contract: the only interactions are the login submit, opening
    + cancelling the category popup, and clicking the popup's Search button
    to drive a search. No filters applied to the listing, no Update click,
    no documents accessed, no portal state changed."""
    from tender_agent.services.bridge_client import make_bridge_client

    bridge = bridge if bridge is not None else make_bridge_client()

    if not await bridge.bridge_available():
        return {
            "available": False,
            "logged_in": False,
            "detail": "browser bridge / Playwright not available",
        }

    try:
        await bridge.open_session(slug, PROACTIS_URLS["login"])
    except Exception as exc:  # noqa: BLE001
        return {
            "available": True,
            "logged_in": False,
            "detail": f"open_session failed: {exc}",
        }

    try:
        attempt = await login_with_credentials(
            bridge, slug=slug, credentials=credentials
        )
        if attempt.status != "ok":
            # Don't try to drive the filter panel without a valid session —
            # the login diagnostic is the right tool for THAT branch.
            return {
                "available": True,
                "logged_in": False,
                "outcome": attempt.status,
                "detail": attempt.detail
                or "login failed — run /proactis/login-diagnostic",
                "cookie_dialog_dismissed": attempt.cookie_dialog_dismissed,
                "cookie_dismiss_method": attempt.cookie_dismiss_method,
            }
        diagnostic = await capture_filter_state(bridge, slug)
        return {
            "available": True,
            "logged_in": True,
            **diagnostic.as_dict(),
        }
    finally:
        try:
            await bridge.close_session(slug)
        except Exception:  # noqa: BLE001
            logger.debug("discovery.proactis.filter_diagnostic_close_failed")
