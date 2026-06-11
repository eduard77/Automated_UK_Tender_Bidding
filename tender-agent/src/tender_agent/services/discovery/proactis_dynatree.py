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

Two facts from the 2026-06-10 follow-up capture shape the DETECTION logic
(the search worked; the old detection misread its success):

* The popup ids (``#TxtFilterNodes``, ``#DivTree``, ``#divNoSearchResults``)
  are NOT unique on the page — every category dialog ever instantiated
  leaves its own copies in the DOM. All reads must be scoped to the
  CURRENTLY OPEN ``.ui-dialog`` (the one without ``display: none``).
* ``#divNoSearchResults`` EXISTS permanently and is merely hidden
  (``display: none``) when a search has results — presence is NOT
  visibility. And the tree loads asynchronously (a ``Loading…`` status node
  shows first), so no verdict may be read before the tree settles.

This module knows ONLY how to:
  1. Scope a rendered page to the open dialog and classify the search state
     (``read_search_state``: loading / results / no_results / pending).
  2. Parse a Dynatree popup's rendered HTML into ``DynatreeNode`` records.
  3. Match a CPV code (e.g. ``45000000``) check-digit-tolerantly against
     node titles like ``45000000-7 - Construction work``.
  4. Match a region name case-insensitively against the title text.
  5. Confirm a tick landed in the ``#DivSelected`` panel.

The bridge orchestration (open popup, type, click search, click checkbox,
click apply) lives in ``proactis_discovery.py``. Keeping the matching logic
here makes it unit-testable against saved HTML with no browser — and the
filter diagnostic uses the SAME functions, so the two can never disagree.
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

# Recognise the no-results indicator's tag. NOTE: the live page keeps this
# div in the DOM permanently and toggles `display: none` — presence alone
# proved to misread a SUCCESSFUL search as a miss (2026-06-10 capture), so
# `no_results_visible` also inspects the tag's inline style.
_NO_RESULTS_RE = re.compile(
    r'<[^>]*id="divNoSearchResults"[^>]*>',
    re.IGNORECASE,
)

# Inline-hidden detection on a single start tag (jQuery's .hide()/.show()
# toggles an inline `display` style).
_INLINE_HIDDEN_RE = re.compile(
    r'style\s*=\s*["\'][^"\']*display\s*:\s*none',
    re.IGNORECASE,
)

# The jQuery-UI dialog WRAPPER start tag — class carries the standalone token
# `ui-dialog` (not `ui-dialog-content` / `ui-dialog-titlebar`, which are
# inner elements). Used to slice the page into per-dialog chunks.
_DIALOG_START_RE = re.compile(
    r'<div[^>]*class\s*=\s*["\'][^"\']*(?<![\w-])ui-dialog(?![\w-])[^"\']*["\'][^>]*>',
    re.IGNORECASE,
)

# Dynatree's transient status row while the search XHR is in flight.
_LOADING_RE = re.compile(
    r'dynatree-statusnode-wait|dynatree-statusnode-loading'
    r'|class="[^"]*\bdynatree-wait\b[^"]*"'
    r'|>\s*Loading(?:…|\.\.\.)?\s*<',
    re.IGNORECASE,
)

# The selected-items panel that fills as nodes are ticked.
_SELECTED_PANEL_RE = re.compile(
    r'<[^>]*id="DivSelected"[^>]*>',
    re.IGNORECASE,
)

# Markers that identify a dialog chunk as THE Dynatree popup (vs. some other
# jQuery-UI dialog — session-timeout warnings, validation alerts — that may
# be open at the same time).
_POPUP_MARKER_RE = re.compile(
    r'id="(?:TxtFilterNodes|DivTree|divNoSearchResults)"|dynatree-container',
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
    """True iff a no-results marker is present AND actually displayed.

    The live popup keeps ``#divNoSearchResults`` in the DOM at all times and
    hides it with an inline ``display: none`` when the search has results —
    so presence alone misread every successful CPV search as a miss (the
    2026-06-10 capture: tree contained ``45000000-7 - Construction work``,
    yet the run logged ``divNoSearchResults visible``)."""
    for match in _NO_RESULTS_RE.finditer(popup_html or ""):
        if not _INLINE_HIDDEN_RE.search(match.group(0)):
            return True
    return False


def scope_to_open_dialog(page_html: str) -> str:
    """Slice the rendered page down to the CURRENTLY OPEN jQuery-UI dialog.

    The popup ids are not unique: every category dialog ever instantiated
    on the page leaves its own ``#DivTree`` / ``#divNoSearchResults`` copies
    behind, so an unscoped read can land on a STALE dialog's marker. Chunks
    run from each ``.ui-dialog`` wrapper start tag to the next; a chunk is
    open iff its wrapper tag is not inline-hidden. Among open dialogs, the
    LAST one that actually CONTAINS the popup markers (search box / tree /
    marker) wins — a session-timeout warning or validation alert appended
    after the popup must not shadow it; with no popup-marked chunk the last
    open dialog wins (most recently opened). No wrapper at all → the input
    is already popup-scoped (tests, older markup) and is returned
    unchanged; wrappers present but ALL hidden → the page content BEFORE
    the first wrapper (jQuery-UI appends dialogs at the end of body, so an
    older un-wrapped popup variant still gets read instead of every search
    degrading to not-settled)."""
    html = page_html or ""
    starts = list(_DIALOG_START_RE.finditer(html))
    if not starts:
        return html
    open_chunks: list[str] = []
    for index, match in enumerate(starts):
        if _INLINE_HIDDEN_RE.search(match.group(0)):
            continue
        end = starts[index + 1].start() if index + 1 < len(starts) else len(html)
        open_chunks.append(html[match.start():end])
    if not open_chunks:
        return html[: starts[0].start()]
    for chunk in reversed(open_chunks):
        if _POPUP_MARKER_RE.search(chunk):
            return chunk
    return open_chunks[-1]


def tree_loading(popup_html: str) -> bool:
    """True while Dynatree's transient ``Loading…`` status row is present —
    the search XHR hasn't returned yet, so NO verdict may be read."""
    return bool(_LOADING_RE.search(popup_html or ""))


@dataclass(frozen=True)
class PopupSearchState:
    """The settled-or-not verdict for one popup search, computed over the
    OPEN dialog only.

    status:
      * ``loading``    — the Loading… status node is still showing; wait.
      * ``results``    — at least one Dynatree node rendered; match + tick.
      * ``no_results`` — zero nodes AND the no-results marker is displayed.
      * ``pending``    — none of the above (dialog not open / not rendered
        yet); wait, and never treat as a miss.
    """

    status: str
    nodes: list[DynatreeNode]
    scope_html: str


def read_search_state(page_html: str) -> PopupSearchState:
    """Classify the popup's search state from full-page rendered HTML.

    Scopes to the open dialog first, then: loading beats everything (the
    tree hasn't settled); ANY rendered node means results — even if a stale
    or mid-toggle no-results marker is also present; the marker only counts
    when it is displayed AND the result list is empty."""
    scope = scope_to_open_dialog(page_html)
    if tree_loading(scope):
        return PopupSearchState(status="loading", nodes=[], scope_html=scope)
    nodes = parse_dynatree_nodes(scope)
    if nodes:
        return PopupSearchState(status="results", nodes=nodes, scope_html=scope)
    if no_results_visible(scope):
        return PopupSearchState(status="no_results", nodes=[], scope_html=scope)
    return PopupSearchState(status="pending", nodes=[], scope_html=scope)


def selection_confirmed(page_html: str, needle: str) -> bool | None:
    """Did the ticked node land in the open dialog's ``#DivSelected`` panel?

    Returns True/False when the panel is present in the open dialog, None
    when it isn't readable (panel absent / dialog not found) — callers treat
    None as "cannot verify", not as failure."""
    target = (needle or "").strip().lower()
    if not target:
        return None
    scope = scope_to_open_dialog(page_html)
    panel = _SELECTED_PANEL_RE.search(scope)
    if panel is None:
        return None
    window = scope[panel.end():]
    stop = re.search(r'id="BtnSelect"', window, re.IGNORECASE)
    if stop is not None:
        window = window[: stop.start()]
    return target in window.lower()


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
    # Fallback: title-prefix tolerance — "45000000(-7)? - label". Covers a
    # title whose leading token the parse-time extractor didn't recognise
    # (unexpected separators / formatting drift around the check digit).
    prefix = re.compile(rf"^\s*{re.escape(target)}(?:-\d)?(?=$|\s|-)")
    for node in nodes:
        if prefix.match(node.title_text or ""):
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
    # as the value — caller MUST clear before typing. The `_scoped` variants
    # pin the interaction to the OPEN dialog (Playwright's `:visible`) —
    # popup ids are NOT unique once more than one dialog has been
    # instantiated on the page; callers try scoped first, bare as fallback.
    "search_input": "input#TxtFilterNodes, input.CategoryTextBox, input[name='search']",
    "search_input_scoped": ".ui-dialog:visible input#TxtFilterNodes",
    "search_button": "input#BtnSearchCat",
    "search_button_scoped": ".ui-dialog:visible input#BtnSearchCat",
    # Default checked — we click it defensively in case a prior popup left
    # FuzzySearch selected.
    "exact_match_radio": "input#radExact",
    "exact_match_radio_scoped": ".ui-dialog:visible input#radExact",
    # Tree + result-state markers we wait on after Search.
    "tree_container": "#DivTree ul.dynatree-container",
    "no_results_marker": "#divNoSearchResults",
    # The wait-on-after-search selector — either the tree settles OR the
    # no-results marker appears. The VERDICT is never taken from this wait
    # alone: `read_search_state` re-reads the scoped, settled DOM.
    "tree_settled_wait": "#DivTree ul.dynatree-container, #divNoSearchResults",
    # The clickable span inside ONE specific node. The caller builds this
    # by formatting `node_checkbox_for(node_id)` once it knows which node.
    "selected_list": "#DivSelected",
    "apply_button": "input#BtnSelect",
    "apply_button_scoped": ".ui-dialog:visible input#BtnSelect",
    "cancel_link": "a.cancel",
}

#: After Search the tree loads ASYNCHRONOUSLY (Loading… status node first).
#: Both the discovery driver and the diagnostic re-read the rendered DOM
#: until `read_search_state` settles (results / no_results), bounded by
#: these. Shared here so the two flows cannot pace differently. The READ
#: timeout is deliberately SHORT: Playwright's wait_for_selector judges
#: only the FIRST DOM match of a selector, so on a page carrying stale
#: hidden dialog copies the wait may never satisfy — the verdict comes
#: from `read_search_state` over the returned DOM, and the loop paces via
#: the retry delay, so a long per-read wait is pure dead time.
TREE_SETTLE_RETRIES = 6
TREE_SETTLE_RETRY_DELAY_S = 0.4
TREE_SETTLE_READ_TIMEOUT_MS = 1500


def node_checkbox_selector(node_id: str, *, scoped: bool = False) -> str:
    """The bridge.click selector for ticking a specific Dynatree node.

    The checkbox is a `<span class="dynatree-checkbox">` INSIDE the `<li>`
    whose id is `node_id`. Building the selector from the captured node_id
    keeps every tick uniquely targeted even when the tree carries dozens
    of expanded rows. `scoped=True` additionally pins the click to the OPEN
    dialog — node ids repeat across stale dialog instances; callers click
    the scoped form first and fall back to the bare one."""
    # Escape any tilde / other CSS-special char with a backslash for use
    # in a CSS attribute selector — Proactis uses literal `~` in node ids,
    # which is a CSS attribute-selector operator and must be quoted.
    base = f'li[id="{node_id}"] span.dynatree-checkbox'
    return f".ui-dialog:visible {base}" if scoped else base
