"""Tests for the operator-run `scripts/connect_delta.py` Delta capture helper.

No live Delta, no real browser, no network: we exercise the pure flow logic —
endpoint targeting, backend login + upload (success and every failure path),
and the temp-state-file cleanup that keeps the captured credential off disk.
Playwright is imported lazily inside the script, so this file imports cleanly
even where Playwright/Chromium aren't installed.
"""
from __future__ import annotations

import importlib.util
import io
import urllib.error
import urllib.request
from pathlib import Path

import pytest

# Load scripts/connect_delta.py (it lives at the repo root, outside the package).
_HELPER = (
    Path(__file__).resolve().parents[2] / "scripts" / "connect_delta.py"
)
_spec = importlib.util.spec_from_file_location("connect_delta", _HELPER)
assert _spec and _spec.loader
cd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cd)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, body: bytes = b"") -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *a: object) -> None:
        return None


def _cookiejar(opener: urllib.request.OpenerDirector):
    proc = next(
        h
        for h in opener.handlers
        if isinstance(h, urllib.request.HTTPCookieProcessor)
    )
    return proc.cookiejar


def _add_session_cookie(opener: urllib.request.OpenerDirector) -> None:
    jar = _cookiejar(opener)
    jar.set_cookie(
        http_cookie(cd.SESSION_COOKIE_NAME, "tok123", "testserver")
    )


def http_cookie(name: str, value: str, domain: str):
    import http.cookiejar

    return http.cookiejar.Cookie(
        version=0, name=name, value=value, port=None, port_specified=False,
        domain=domain, domain_specified=True, domain_initial_dot=False,
        path="/", path_specified=True, secure=False, expires=None,
        discard=False, comment=None, comment_url=None, rest={},
    )


def _http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://x", code, "err", {}, io.BytesIO(body)
    )


# ---------------------------------------------------------------------------
# Endpoint targeting / constants
# ---------------------------------------------------------------------------


def test_targets_the_deployed_backend_and_right_paths():
    assert cd.DEFAULT_BACKEND_URL == (
        "https://generatender-gqbgaye9fmdfc4c6.ukwest-01.azurewebsites.net"
    )
    assert cd.LOGIN_PATH == "/accounts/login"
    assert cd.UPLOAD_PATH == "/admin/portals/delta/session"
    assert cd.SESSION_COOKIE_NAME == "tender_agent_session"


def test_delta_markers_loaded_and_sane():
    # Whether from the canonical adapter or the inlined mirror, these must be set.
    assert "delta-esourcing.com" in cd.DELTA_LOGIN_URL
    assert "Response Manager" in cd.DELTA_LOGGED_IN_MARKER
    # The success pattern must match the CONFIRMED logged-in landing (mainMenu)
    # and reject the login page — and must NOT require /delta/suppliers/.
    import re as _re

    rx = _re.compile(cd.DELTA_SUCCESS_URL_PATTERN, _re.IGNORECASE)
    assert rx.search("https://www.delta-esourcing.com/delta/mainMenu.html")
    assert not rx.search("https://www.delta-esourcing.com/delta/login.html")


def test_markers_match_canonical_adapter():
    # The reuse contract: the helper's markers equal the adapter's constants.
    import sys

    sys.path.insert(
        0, str(Path(__file__).resolve().parents[1] / "src")
    )
    from tender_agent.services.portals.adapters.delta_esourcing import (
        DELTA_LOGIN_SUCCESS_PATTERN,
        DELTA_SELECTORS,
        DELTA_URLS,
    )

    assert DELTA_URLS["main_menu"] == cd.DELTA_LOGIN_URL
    assert DELTA_SELECTORS["logged_in_marker"] == cd.DELTA_LOGGED_IN_MARKER
    assert cd.DELTA_SUCCESS_URL_PATTERN == DELTA_LOGIN_SUCCESS_PATTERN


# ---------------------------------------------------------------------------
# backend_login
# ---------------------------------------------------------------------------


def test_backend_login_success_stores_cookie():
    opener = cd.make_opener()

    def fake_open(req, timeout=0):
        _add_session_cookie(opener)
        return _FakeResp(b"{}")

    opener.open = fake_open  # type: ignore[assignment]
    cd.backend_login(opener, "http://testserver", "op@x.com", "pw")
    assert cd._has_session_cookie(opener)


def test_backend_login_bad_credentials_raises_friendly():
    opener = cd.make_opener()
    opener.open = lambda req, timeout=0: (_ for _ in ()).throw(  # type: ignore[assignment]
        _http_error(401)
    )
    with pytest.raises(cd.ConnectError, match="rejected"):
        cd.backend_login(opener, "http://testserver", "op@x.com", "bad")


def test_backend_login_network_failure_raises_friendly():
    opener = cd.make_opener()
    opener.open = lambda req, timeout=0: (_ for _ in ()).throw(  # type: ignore[assignment]
        urllib.error.URLError("no route")
    )
    with pytest.raises(cd.ConnectError, match="reach the backend"):
        cd.backend_login(opener, "http://testserver", "op@x.com", "pw")


def test_backend_login_missing_cookie_raises():
    opener = cd.make_opener()
    opener.open = lambda req, timeout=0: _FakeResp(b"{}")  # type: ignore[assignment]
    with pytest.raises(cd.ConnectError, match="no session cookie"):
        cd.backend_login(opener, "http://testserver", "op@x.com", "pw")


# ---------------------------------------------------------------------------
# upload_session
# ---------------------------------------------------------------------------


def test_upload_session_success_returns_payload(tmp_path: Path):
    state = tmp_path / "s.json"
    state.write_text('{"cookies": [], "origins": []}')
    opener = cd.make_opener()
    opener.open = lambda req, timeout=0: _FakeResp(  # type: ignore[assignment]
        b'{"ok": true, "slug": "delta_esourcing", "updated_at": "2026-06-02T00:00:00"}'
    )
    out = cd.upload_session(opener, "http://testserver", state)
    assert out["ok"] is True
    assert out["slug"] == "delta_esourcing"


def test_upload_session_auth_failure_raises_friendly(tmp_path: Path):
    state = tmp_path / "s.json"
    state.write_text("{}")
    opener = cd.make_opener()
    opener.open = lambda req, timeout=0: (_ for _ in ()).throw(  # type: ignore[assignment]
        _http_error(401)
    )
    with pytest.raises(cd.ConnectError, match="rejected"):
        cd.upload_session(opener, "http://testserver", state)


def test_upload_session_400_surfaces_detail(tmp_path: Path):
    state = tmp_path / "s.json"
    state.write_text("{}")
    opener = cd.make_opener()
    opener.open = lambda req, timeout=0: (_ for _ in ()).throw(  # type: ignore[assignment]
        _http_error(400, b'{"detail": "storage_state.cookies must be a list"}')
    )
    with pytest.raises(cd.ConnectError, match="cookies must be a list"):
        cd.upload_session(opener, "http://testserver", state)


def test_upload_session_network_failure_raises_friendly(tmp_path: Path):
    state = tmp_path / "s.json"
    state.write_text("{}")
    opener = cd.make_opener()
    opener.open = lambda req, timeout=0: (_ for _ in ()).throw(  # type: ignore[assignment]
        urllib.error.URLError("down")
    )
    with pytest.raises(cd.ConnectError, match="couldn't reach"):
        cd.upload_session(opener, "http://testserver", state)


# ---------------------------------------------------------------------------
# Temp-file lifecycle — the captured session must never be left on disk.
# ---------------------------------------------------------------------------


def test_tempfile_created_and_shredded():
    path = cd.new_state_tempfile()
    assert path.exists()
    cd.shred_tempfile(path)
    assert not path.exists()


def test_shred_is_safe_when_missing(tmp_path: Path):
    cd.shred_tempfile(tmp_path / "nope.json")  # must not raise


def test_run_always_shreds_state_file_even_on_failure(monkeypatch):
    created: dict[str, Path] = {}
    real_new = cd.new_state_tempfile

    def fake_new() -> Path:
        p = real_new()
        created["p"] = p
        return p

    monkeypatch.setattr(cd, "new_state_tempfile", fake_new)
    # Fail early at the credential prompt so nothing real runs.
    monkeypatch.setattr(
        cd, "prompt_credentials",
        lambda email: (_ for _ in ()).throw(cd.ConnectError("boom")),
    )
    args = cd.parse_args([])
    rc = cd.run(args)
    assert rc == 1
    assert created["p"] and not created["p"].exists()


# ---------------------------------------------------------------------------
# CLI defaults
# ---------------------------------------------------------------------------


def test_parse_args_defaults(monkeypatch):
    monkeypatch.delenv("TENDER_BACKEND_URL", raising=False)
    monkeypatch.delenv("TENDER_OPERATOR_EMAIL", raising=False)
    args = cd.parse_args([])
    assert args.backend_url == cd.DEFAULT_BACKEND_URL
    assert args.email is None
    assert args.timeout == cd.DEFAULT_LOGIN_TIMEOUT_S
    assert args.keep_open is False
    assert args.debug is False
    assert cd.parse_args(["--debug"]).debug is True


# ---------------------------------------------------------------------------
# Login detection — the capture must mirror the adapter's is_authenticated:
# authenticated /delta area (the CONFIRMED mainMenu landing) + NOT login +
# marker visible, plus a real Delta cookie.
# ---------------------------------------------------------------------------

import re  # noqa: E402

# The CONFIRMED logged-in landing — Delta lands here after login + MFA, NOT
# under /delta/suppliers/. This is the exact URL the earlier (too-strict) guard
# rejected, so it's the key regression case.
_MAINMENU_URL = "https://www.delta-esourcing.com/delta/mainMenu.html"
_SUPPLIER_URL = (
    "https://www.delta-esourcing.com/delta/suppliers/select/addToList.html"
)
_LOGIN_URL = "https://www.delta-esourcing.com/delta/login.html"
_SUCCESS_RE = re.compile(cd.DELTA_SUCCESS_URL_PATTERN, re.IGNORECASE)


class _FakeMatch:
    def __init__(self, visible: bool) -> None:
        self._visible = visible

    def is_visible(self, timeout: int = 0) -> bool:
        return self._visible


class _FakeLocator:
    """Models a selector with N matches (each with a visibility flag). `raises`
    makes .count() blow up, to exercise the silent-exception surfacing."""

    def __init__(self, matches: list[bool], raises: bool = False) -> None:
        self._matches = matches
        self._raises = raises
        self.first = self  # legacy attribute; new code uses count()/nth()

    def count(self) -> int:
        if self._raises:
            raise RuntimeError("locator boom")
        return len(self._matches)

    def nth(self, i: int) -> _FakeMatch:
        return _FakeMatch(self._matches[i])

    def is_visible(self, timeout: int = 0) -> bool:  # legacy .first.is_visible
        if self._raises:
            raise RuntimeError("boom")
        return any(self._matches)


class _FakeFrame:
    def __init__(self, matches: list[bool], raises: bool = False) -> None:
        self._matches = matches
        self._raises = raises

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self._matches, self._raises)


class _FakePage:
    def __init__(
        self,
        url: str,
        *,
        marker_visible: bool = True,
        marker_raises: bool = False,
        marker_matches: list[bool] | None = None,
        goto_url: str | None = None,
        frames: list | None = None,
        closed: bool = False,
    ) -> None:
        self._url = url
        # marker_matches takes precedence; else a single match = marker_visible.
        self._matches = (
            marker_matches if marker_matches is not None else [marker_visible]
        )
        self._marker_raises = marker_raises
        self._goto_url = goto_url
        self._frames = frames or []
        self._closed = closed

    @property
    def url(self) -> str:
        return self._url

    @property
    def frames(self) -> list:
        return self._frames

    def is_closed(self) -> bool:
        return self._closed

    def goto(self, url: str, **kw: object) -> None:
        self._url = self._goto_url if self._goto_url is not None else url

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self._matches, self._marker_raises)


class _FakeContext:
    def __init__(self, cookies: list[dict], pages: list | None = None) -> None:
        self._cookies = cookies
        self.pages = pages or []

    def cookies(self) -> list[dict]:
        return self._cookies


# --- _looks_authenticated (cheap, non-navigating gate) ---------------------


def test_looks_authenticated_accepts_real_mainmenu_landing():
    # REGRESSION: a fully logged-in supplier lands on /delta/mainMenu.html (not
    # /delta/suppliers/). The old guard required "suppliers" and never matched.
    page = _FakePage(_MAINMENU_URL, marker_visible=True)
    assert cd._looks_authenticated(page, _SUCCESS_RE) is True


def test_looks_authenticated_rejects_login_page_even_with_marker():
    # The other bug: marker visible but we're on the login page.
    page = _FakePage(_LOGIN_URL, marker_visible=True)
    assert cd._looks_authenticated(page, _SUCCESS_RE) is False


def test_looks_authenticated_rejects_non_app_url():
    page = _FakePage("https://www.delta-esourcing.com/", marker_visible=True)
    assert cd._looks_authenticated(page, _SUCCESS_RE) is False


def test_looks_authenticated_requires_marker_in_app_area():
    assert cd._looks_authenticated(
        _FakePage(_MAINMENU_URL, marker_visible=True), _SUCCESS_RE
    ) is True
    assert cd._looks_authenticated(
        _FakePage(_MAINMENU_URL, marker_visible=False), _SUCCESS_RE
    ) is False
    # Supplier-area pages still count as authenticated.
    assert cd._looks_authenticated(
        _FakePage(_SUPPLIER_URL, marker_visible=True), _SUCCESS_RE
    ) is True


# --- _confirm_authenticated (authoritative, navigates like the probe) ------


def test_confirm_authenticated_true_when_mainmenu_and_marker():
    page = _FakePage(_LOGIN_URL, marker_visible=True, goto_url=_MAINMENU_URL)
    assert cd._confirm_authenticated(page, _SUCCESS_RE) is True


def test_confirm_authenticated_false_when_bounced_to_login():
    page = _FakePage(_MAINMENU_URL, marker_visible=True, goto_url=_LOGIN_URL)
    assert cd._confirm_authenticated(page, _SUCCESS_RE) is False


def test_confirm_authenticated_false_when_marker_never_renders():
    page = _FakePage(
        _LOGIN_URL, marker_visible=False, marker_raises=True, goto_url=_MAINMENU_URL
    )
    # confirm_wait_s=0 → one check then give up (no real 8s wait in the test).
    assert cd._confirm_authenticated(page, _SUCCESS_RE, confirm_wait_s=0) is False


# --- multi-tab / frames / hidden-match — the #92 root-cause fixes -----------


def test_scan_finds_authenticated_tab_among_many():
    # ROOT CAUSE: login (SSO) lands mainMenu in a SECOND tab; the original tab
    # is parked on the login page. Scanning all tabs must find the logged-in one.
    login_tab = _FakePage(_LOGIN_URL, marker_visible=True)
    main_tab = _FakePage(_MAINMENU_URL, marker_visible=True)
    found = cd._scan_for_authenticated_page([login_tab, main_tab], _SUCCESS_RE)
    assert found is main_tab


def test_scan_returns_none_when_no_tab_authenticated():
    tabs = [
        _FakePage(_LOGIN_URL, marker_visible=True),
        _FakePage(_MAINMENU_URL, marker_visible=False),
    ]
    assert cd._scan_for_authenticated_page(tabs, _SUCCESS_RE) is None


def test_scan_skips_closed_tabs():
    closed_main = _FakePage(_MAINMENU_URL, marker_visible=True, closed=True)
    assert cd._scan_for_authenticated_page([closed_main], _SUCCESS_RE) is None


def test_marker_found_inside_a_frame():
    # FRAMES: the page DOM has no visible marker, but a frame does.
    page = _FakePage(
        _MAINMENU_URL,
        marker_visible=False,
        frames=[_FakeFrame(matches=[True])],
    )
    assert cd._looks_authenticated(page, _SUCCESS_RE) is True


def test_marker_scan_ignores_hidden_first_match():
    # HIDDEN .first: the first OR-match is hidden, a later one is visible. The
    # old `.first.is_visible()` would read the hidden one and miss the menu.
    page = _FakePage(_MAINMENU_URL, marker_matches=[False, False, True])
    assert cd._looks_authenticated(page, _SUCCESS_RE) is True


def test_page_authenticated_surfaces_marker_error_in_detail():
    # SILENT EXCEPTIONS: a locator error is reported (for --debug), not swallowed.
    page = _FakePage(_MAINMENU_URL, marker_raises=True)
    ok, detail = cd._page_authenticated(page, _SUCCESS_RE)
    assert ok is False
    assert "err=" in detail and "boom" in detail


def test_scan_debug_sink_lists_every_tab():
    tabs = [
        _FakePage(_LOGIN_URL, marker_visible=True),
        _FakePage(_MAINMENU_URL, marker_visible=True),
    ]
    sink: list[str] = []
    cd._scan_for_authenticated_page(tabs, _SUCCESS_RE, sink)
    assert len(sink) == 2  # debug enumerates ALL tabs, not just up to the match
    assert any("url_ok=False" in line for line in sink)  # the login tab
    assert any("marker=True" in line for line in sink)   # the mainMenu tab


# --- _delta_session_cookie_present (safety net) ----------------------------


def _cookie(name: str, domain: str, value: str = "x") -> dict:
    return {"name": name, "domain": domain, "value": value}


def test_cookie_gate_true_for_jsessionid_on_delta_domain():
    ctx = _FakeContext([_cookie("JSESSIONID", "www.delta-esourcing.com")])
    assert cd._delta_session_cookie_present(ctx) is True


def test_cookie_gate_true_for_any_session_named_delta_cookie():
    ctx = _FakeContext([_cookie("DeltaSessionToken", ".delta-esourcing.com")])
    assert cd._delta_session_cookie_present(ctx) is True


def test_cookie_gate_false_for_loadbalancer_cookie_only():
    # An anonymous capture often still has LB cookies but no session cookie.
    ctx = _FakeContext([_cookie("AWSALB", "www.delta-esourcing.com")])
    assert cd._delta_session_cookie_present(ctx) is False


def test_cookie_gate_false_for_session_cookie_on_other_domain():
    ctx = _FakeContext([_cookie("JSESSIONID", "login.microsoftonline.com")])
    assert cd._delta_session_cookie_present(ctx) is False


def test_cookie_gate_false_when_empty():
    assert cd._delta_session_cookie_present(_FakeContext([])) is False
