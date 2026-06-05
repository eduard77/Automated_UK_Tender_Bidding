"""Frame-aware login-marker detection in the in-process bridge — the backend
port of the capture helper's fix (PR #92), so the headless cloud probe sees a
logged-in session exactly the way the operator's local capture does.

These exercise the pure detection helpers (and `probe_login_markers`) against
duck-typed async fakes — no Playwright, no browser. The three failure modes #92
fixed are pinned here: (a) a marker inside a FRAME is found; (b) a hidden first
match no longer masks a visible later one; (c) locator errors are surfaced, not
swallowed.
"""
from __future__ import annotations

import pytest

from tender_agent.services.bridge_in_process import (
    InProcessBridgeClient,
    _marker_scopes,
    _marker_visible_in_scope,
    _scan_login_markers,
)

pytestmark = pytest.mark.asyncio


# --- duck-typed async Playwright fakes -------------------------------------


class _Nth:
    def __init__(self, val):
        self._val = val  # bool, or an Exception to raise on is_visible()

    async def is_visible(self):
        if isinstance(self._val, Exception):
            raise self._val
        return bool(self._val)


class _Locator:
    def __init__(self, spec):
        # spec: list of per-match values (bool/Exception), OR an Exception to
        # raise from count() (a locator error).
        self._spec = spec

    async def count(self):
        if isinstance(self._spec, Exception):
            raise self._spec
        return len(self._spec)

    def nth(self, i):
        return _Nth(self._spec[i])


class _Scope:
    """A page or frame: maps selector -> match spec. Unknown selectors match
    nothing (empty list)."""

    def __init__(self, matches):
        self._matches = matches

    def locator(self, selector):
        return _Locator(self._matches.get(selector, []))


class _Frame(_Scope):
    def __init__(self, matches, *, name=None, url=None):
        super().__init__(matches)
        self.name = name
        self.url = url


class _Page(_Scope):
    """A page that is its own main-frame scope, plus optional child frames."""

    def __init__(self, matches, *, frames=None, title="Activity Centre | Delta",
                 url="https://www.delta-esourcing.com/delta/mainMenu.html"):
        super().__init__(matches)
        self._frames = frames or []
        self._title = title
        self.url = url
        self.main_frame = self

    @property
    def frames(self):
        # Playwright's page.frames includes the main frame (the page) first.
        return [self, *self._frames]

    async def title(self):
        return self._title


SEL_A = "a:has-text('Response Manager')"
SEL_B = "a:has-text('Settings')"
MARKERS = [{"label": "Response Manager", "selector": SEL_A},
           {"label": "Settings", "selector": SEL_B}]


# --- _marker_scopes --------------------------------------------------------


async def test_marker_scopes_includes_page_and_child_frames_once():
    fr = _Frame({}, name="menuFrame")
    page = _Page({}, frames=[fr])
    scopes = _marker_scopes(page)
    names = [n for n, _ in scopes]
    # The page is 'main'; the child frame is included once (named); the main
    # frame is NOT double-counted even though page.frames lists it.
    assert names == ["main", "menuFrame"]


async def test_marker_scopes_labels_frame_by_url_when_unnamed():
    fr = _Frame({}, name=None, url="https://x/inner")
    page = _Page({}, frames=[fr])
    assert [n for n, _ in _marker_scopes(page)] == ["main", "https://x/inner"]


# --- _marker_visible_in_scope (every match, errors surfaced) ---------------


async def test_hidden_first_match_does_not_mask_visible_later_one():
    # The OR-selector resolves to a hidden element first, then a visible one —
    # checking EVERY match (not `.first`) must still report visible (#92 (b)).
    scope = _Scope({SEL_A: [False, True]})
    count, visible, err = await _marker_visible_in_scope(scope, SEL_A)
    assert (count, visible, err) == (2, True, None)


async def test_all_hidden_matches_report_not_visible():
    scope = _Scope({SEL_A: [False, False]})
    count, visible, err = await _marker_visible_in_scope(scope, SEL_A)
    assert (count, visible) == (2, False)


async def test_locator_error_is_surfaced_not_swallowed():
    # count() raising (a locator/timeout error) is returned, not swallowed (#92 (c)).
    boom = RuntimeError("locator exploded")
    scope = _Scope({SEL_A: boom})
    count, visible, err = await _marker_visible_in_scope(scope, SEL_A)
    assert count == 0 and visible is False
    assert err is not None and "locator exploded" in err


async def test_is_visible_error_surfaced_when_no_match_visible():
    scope = _Scope({SEL_A: [RuntimeError("detached")]})
    _, visible, err = await _marker_visible_in_scope(scope, SEL_A)
    assert visible is False
    assert err is not None and "detached" in err


# --- _scan_login_markers (page + frames) -----------------------------------


async def test_marker_found_inside_a_frame():
    # Nothing visible on the top document; the supplier menu lives in a frame —
    # it must still be detected (#92 (a)), and reported in that frame.
    fr = _Frame({SEL_A: [True]}, name="menuFrame")
    page = _Page({}, frames=[fr])
    alts, any_visible, err = await _scan_login_markers(page, MARKERS)
    assert any_visible is True
    assert err is None
    rm = next(a for a in alts if a.label == "Response Manager")
    assert rm.found is True and rm.visible is True and rm.frame == "menuFrame"


async def test_marker_on_main_document_reported_as_main():
    page = _Page({SEL_A: [True]})
    alts, any_visible, _ = await _scan_login_markers(page, MARKERS)
    rm = next(a for a in alts if a.label == "Response Manager")
    assert any_visible is True and rm.frame == "main"


async def test_present_but_hidden_marker_is_found_not_visible():
    # Found in DOM but not visible (e.g. collapsed nav) → found True, visible
    # False, and that distinction is reported per-alternative.
    page = _Page({SEL_A: [False]})
    alts, any_visible, _ = await _scan_login_markers(page, MARKERS)
    rm = next(a for a in alts if a.label == "Response Manager")
    assert any_visible is False
    assert rm.found is True and rm.visible is False and rm.frame == "main"


async def test_no_markers_anywhere_all_false():
    page = _Page({}, frames=[_Frame({}, name="f")])
    alts, any_visible, err = await _scan_login_markers(page, MARKERS)
    assert any_visible is False and err is None
    assert all(not a.found and not a.visible for a in alts)


async def test_scan_surfaces_first_error():
    page = _Page({SEL_A: RuntimeError("kaboom")})
    alts, any_visible, err = await _scan_login_markers(page, MARKERS)
    assert any_visible is False
    assert err is not None and "kaboom" in err
    rm = next(a for a in alts if a.label == "Response Manager")
    assert rm.error is not None and "kaboom" in rm.error


# --- probe_login_markers (the public bridge method) ------------------------


def _client_with_page(page):
    client = InProcessBridgeClient()

    class _Session:
        pass

    sess = _Session()
    sess.page = page
    # Bypass the real manager: feed our fake session directly.
    client._manager.get = lambda slug: sess  # type: ignore[assignment]
    return client


async def test_probe_login_markers_returns_rich_result():
    fr = _Frame({SEL_B: [False, True]}, name="menuFrame")  # hidden-then-visible
    page = _Page({}, frames=[fr], title="Activity Centre | Delta")
    client = _client_with_page(page)
    res = await client.probe_login_markers(
        "delta_esourcing", MARKERS, timeout_ms=0
    )
    assert res.visible is True
    assert res.page_title == "Activity Centre | Delta"
    assert res.current_url.endswith("/delta/mainMenu.html")
    assert res.frames_searched == ["main", "menuFrame"]
    settings_alt = next(a for a in res.alternatives if a.label == "Settings")
    assert settings_alt.visible is True and settings_alt.frame == "menuFrame"


async def test_probe_login_markers_false_when_nothing_visible():
    page = _Page({})
    client = _client_with_page(page)
    res = await client.probe_login_markers(
        "delta_esourcing", MARKERS, timeout_ms=0
    )
    assert res.visible is False
    assert res.frames_searched == ["main"]
    assert all(not a.visible for a in res.alternatives)
