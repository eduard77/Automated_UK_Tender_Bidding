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


class _FakeLocator:
    def __init__(self, visible: bool, raises: bool) -> None:
        self._visible = visible
        self._raises = raises
        self.first = self

    def is_visible(self, timeout: int = 0) -> bool:
        if self._raises:
            raise RuntimeError("boom")
        return self._visible

    def wait_for(self, state: str = "visible", timeout: int = 0) -> None:
        if self._raises or not self._visible:
            raise RuntimeError("not visible")


class _FakePage:
    def __init__(
        self,
        url: str,
        *,
        marker_visible: bool = True,
        marker_raises: bool = False,
        goto_url: str | None = None,
    ) -> None:
        self._url = url
        self._marker_visible = marker_visible
        self._marker_raises = marker_raises
        self._goto_url = goto_url

    @property
    def url(self) -> str:
        return self._url

    def goto(self, url: str, **kw: object) -> None:
        self._url = self._goto_url if self._goto_url is not None else url

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self._marker_visible, self._marker_raises)


class _FakeContext:
    def __init__(self, cookies: list[dict]) -> None:
        self._cookies = cookies

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
    assert cd._confirm_authenticated(page, _SUCCESS_RE) is False


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
