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
    assert "suppliers" in cd.DELTA_SUCCESS_URL_PATTERN


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

    assert DELTA_URLS["response_manager"] == cd.DELTA_LOGIN_URL
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
