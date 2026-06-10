"""Dynatree popup helpers — pure logic over saved HTML.

Proactis's Categories and Regions filters are jQuery-UI dialogs containing a
Dynatree widget. The captured popup structure (live, with the operator's
authenticated session) is:

* Search box: ``input#TxtFilterNodes`` (placeholder pre-filled — must be cleared).
* Search button: ``input#BtnSearchCat``.
* Match-mode radios: ``#radExact`` (default) / ``#radFuzzy``. We use Exact.
* Tree container: ``#DivTree ul.dynatree-container``.
* Each node: ``li[id^='node~']`` containing ``span.dynatree-node`` with:
  - ``span.dynatree-checkbox`` — clicking this SPAN ticks the box (it is NOT a
    real ``<input type=checkbox>``).
  - ``a.dynatree-title`` — text like ``45000000-7 - Construction work``. The
    leading token before the first space is the CPV code, possibly with a
    ``-CHECKDIGIT`` suffix.
* Selected list: ``#DivSelected`` (fills as you tick).
* Apply button: ``input#BtnSelect`` (label "Select categories" / "Select regions").
* No-results marker: ``#divNoSearchResults``.

This module knows ONLY how to:
  1. Parse a Dynatree popup's rendered HTML into ``DynatreeNode`` records.
  2. Match a CPV code (e.g. ``45000000``) suffix-insensitively against the
     leading token of each title.
  3. Match a region name case-insensitively against the title text.

The bridge orchestration (open popup, type, click search, click checkbox,
click apply) lives in ``proactis_discovery.py``. Keeping the matching logic
here makes it unit-testable against saved HTML with no browser.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Match a single Dynatree node row. We deliberately capture ATTRIBUTE chunks
# rather than relying on a full HTML parse — Proactis dropped the surrounding
# markup variations onto us at one point already (chunk 9 selector audit
# 2026-05) so the looser regex saves a future audit when ids drift.
#
# Pattern: <li id="node~..." ...> ... <a class="dynatree-title" ...>TITLE</a>
_NODE_RE = re.compile(
    r'<li[^>]*id="(?P<node_id>node[^"]+)"[^>]*>'
    r'(?P<inner>.*?)'
    r'</li>',
    re.IGNORECASE | re.DOTALL,
)

_TITLE_RE = re.compile(
    r'<a[^>]*class="[^"]*dynatree-title[^"]*"[^>]*>(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)

# Recognise the no-results indicator (visibility is best-effort; if the div is
# present in the HTML but `display:none`, we still treat the search as empty —
# the bridge orchestrator confirms by checking for ANY matching node).
_NO_RESULTS_RE = re.compile(
    r'<[^>]*id="divNoSearchResults"[^>]*>',
    re.IGNORECASE,
)

# The leading code token in a CPV title: digits, optionally followed by a
# `-checkdigit` suffix, terminated by space or hyphen-space.
_CODE_TOKEN_RE = re.compile(r"^\s*(?P<digits>\d{2,10})(?:-\d)?\b")

_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class DynatreeNode:
    """One row inside the popup's tree.

    `code_token` is the leading 2–10-digit code on the title (None if the
    title doesn't start with digits — typical for regions). `title_text`
    is the visible title with HTML tags stripped, used for region matching.
    """

    node_id: str
    title_text: str
    code_token: str | None


def parse_dynatree_nodes(popup_html: str) -> list[DynatreeNode]:
    """Extract every Dynatree node from the popup HTML.

    A node has an `id="node~..."`-shaped attribute on its `<li>` and an
    `<a class="dynatree-title">` somewhere inside. Returns the empty list if
    no rows match — the caller treats that as "search returned nothing".
    """
    out: list[DynatreeNode] = []
    for match in _NODE_RE.finditer(popup_html):
        inner = match.group("inner")
        title_match = _TITLE_RE.search(inner)
        if title_match is None:
            continue
        title_text = _TAG_RE.sub("", title_match.group("title")).strip()
        code_token = None
        token_match = _CODE_TOKEN_RE.match(title_text)
        if token_match is not None:
            code_token = token_match.group("digits")
        out.append(
            DynatreeNode(
                node_id=match.group("node_id"),
                title_text=title_text,
                code_token=code_token,
            )
        )
    return out


def no_results_visible(popup_html: str) -> bool:
    """True iff the no-results marker is present anywhere in the rendered
    popup HTML. Useful as a positive 'search returned zero' signal even when
    the search bar itself echoed back a populated tree from a previous query."""
    return bool(_NO_RESULTS_RE.search(popup_html))


def normalise_code(raw: str) -> str:
    """Strip whitespace and any trailing `-checkdigit` so `45000000-7`
    compares equal to `45000000`. Used by both the search-term we type and
    the title-match we then look for."""
    cleaned = (raw or "").strip()
    if not cleaned:
        return ""
    # Drop a trailing -checkdigit if present.
    return re.sub(r"-\d$", "", cleaned)


def expand_cpv_prefix(raw: str) -> str:
    """Expand a CPV prefix to the 8-digit code form the CPV tree displays.

    The operator's FilterProfile stores DIVISION-level prefixes ("45", "71",
    "09"); the live CPV tree titles nodes with full codes ("45000000-7 -
    Construction work") and — per the 2026-06-10 filter-diagnostic — the
    popup search matches code PREFIXES even in Exact mode. So both the
    search term we type and the node-match target use the canonical 8-digit
    form: "45" -> "45000000", "451" -> "45100000". Full codes and
    non-numeric labels pass through unchanged (minus any -checkdigit, which
    `normalise_code` already strips)."""
    cleaned = normalise_code(raw)
    if cleaned.isdigit() and 2 <= len(cleaned) < 8:
        return cleaned.ljust(8, "0")
    return cleaned


def match_node_for_code(
    nodes: list[DynatreeNode], code: str
) -> DynatreeNode | None:
    """Find the node whose title starts with `code` (suffix-insensitive).

    `45000000` matches a title `45000000-7 - Construction work`. We compare
    on the code TOKEN extracted at parse time so we don't have to re-strip on
    every lookup, and a node with no leading code token (e.g. a region row
    that landed in a CPV popup by accident) never spuriously matches a CPV
    code lookup.
    """
    target = normalise_code(code)
    if not target:
        return None
    for node in nodes:
        if node.code_token is None:
            continue
        if normalise_code(node.code_token) == target:
            return node
    return None


def match_node_for_region(
    nodes: list[DynatreeNode], region: str
) -> DynatreeNode | None:
    """Find the node whose visible title is the requested region.

    Comparison is case-insensitive on the leading word(s) of the title.
    Region names in the profile may be informal ("North West") and Proactis
    titles may carry the country prefix ("North West (England)"), so we
    match on title startswith the requested region after lowercasing.
    """
    target = (region or "").strip().lower()
    if not target:
        return None
    for node in nodes:
        title = (node.title_text or "").lower()
        if title.startswith(target):
            return node
        # Loose match: the region appears as a whole word anywhere in the
        # title. Belt-and-braces for cases like "England — North West".
        if target in title and len(target) >= 3:
            return node
    return None


# ---------------------------------------------------------------------------
# Selectors used by the bridge orchestrator. Module-level so a popup-DOM
# audit lands in ONE place and the discovery code stays declarative.
# ---------------------------------------------------------------------------

DYNATREE_SELECTORS = {
    # Search box: the input that filters the tree. Has a placeholder pre-set
    # as the value — caller MUST clear before typing.
    "search_input": "input#TxtFilterNodes, input.CategoryTextBox, input[name='search']",
    "search_button": "input#BtnSearchCat",
    # Default checked — we click it defensively in case a prior popup left
    # FuzzySearch selected.
    "exact_match_radio": "input#radExact",
    # Tree + result-state markers we wait on after Search.
    "tree_container": "#DivTree ul.dynatree-container",
    "no_results_marker": "#divNoSearchResults",
    # The wait-on-after-search selector — either the tree settles OR the
    # no-results marker appears.
    "tree_settled_wait": "#DivTree ul.dynatree-container, #divNoSearchResults",
    # The clickable span inside ONE specific node. The caller builds this
    # by formatting `node_checkbox_for(node_id)` once it knows which node.
    "selected_list": "#DivSelected",
    "apply_button": "input#BtnSelect",
    "cancel_link": "a.cancel",
}


def node_checkbox_selector(node_id: str) -> str:
    """The bridge.click selector for ticking a specific Dynatree node.

    The checkbox is a `<span class="dynatree-checkbox">` INSIDE the `<li>`
    whose id is `node_id`. Building the selector from the captured node_id
    keeps every tick uniquely targeted even when the tree carries dozens
    of expanded rows."""
    # Escape any tilde / other CSS-special char with a backslash for use
    # in a CSS attribute selector — Proactis uses literal `~` in node ids,
    # which is a CSS attribute-selector operator and must be quoted.
    return f'li[id="{node_id}"] span.dynatree-checkbox'
