#!/usr/bin/env python3
"""Connect Delta — friendly, operator-run session capture (login model B).

WHAT THIS IS
    A guided helper an operator runs ON THEIR OWN LAPTOP. It opens a real,
    visible browser at Delta's login page, lets you log in by hand (including
    Microsoft Authenticator MFA), detects when login succeeded, captures the
    Playwright session (`storage_state`), and uploads it to the cloud backend.
    It replaces the old "playwright codegen + curl" route.

    No password is stored. The captured session is treated as a credential:
    it is written to a temp file, uploaded, then DELETED. Nothing is logged.

ONE-TIME SETUP (do this once per laptop)
    pip install playwright==1.47.0
    playwright install chromium

RUN IT (the common case — no flags needed)
    python scripts/connect_delta.py

    You will be asked for your operator email + password (to sign in to the
    cloud backend). Then a browser opens — log into Delta as you normally
    would, finish the Authenticator prompt, and come back. The rest is
    automatic.

OPTIONS (rarely needed)
    --backend-url URL   Cloud backend (default: the deployed UK West app).
    --email EMAIL       Operator email (otherwise you'll be prompted).
    --timeout SECONDS   How long to wait for you to finish logging in
                        (default: 600 = 10 minutes).
    --keep-open         Leave the browser open after capture (debugging).
    --debug             Print per-iteration diagnostics (open tabs + URLs, the
                        marker result per tab/frame, the cookie gate) if login
                        is never detected.

Backend auth: the upload endpoint (POST /admin/portals/delta/session) is behind
`require_account`, so we first POST your email/password to /accounts/login,
keep the returned `tender_agent_session` cookie, and send it with the upload.
The password is used only for that one login call and is never stored or logged.
"""
from __future__ import annotations

import argparse
import contextlib
import getpass
import http.cookiejar
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Default deployed cloud backend (UK West Azure App Service).
DEFAULT_BACKEND_URL = (
    "https://generatender-gqbgaye9fmdfc4c6.ukwest-01.azurewebsites.net"
)

# Login-state plumbing.
LOGIN_PATH = "/accounts/login"
UPLOAD_PATH = "/admin/portals/delta/session"
SESSION_COOKIE_NAME = "tender_agent_session"  # mirrors config.session_cookie_name

# How long to wait for the human to finish logging in (Authenticator included).
DEFAULT_LOGIN_TIMEOUT_S = 600

# ---------------------------------------------------------------------------
# Delta login markers.
#
# These are reused from the backend's Delta adapter so the helper detects
# "logged in" exactly the way the adapter does. When run from inside the repo
# we import the canonical constants; on a bare operator laptop (no backend
# installed) we fall back to the inlined MIRROR below. Keep the mirror in sync
# with tender-agent/src/tender_agent/services/portals/adapters/delta_esourcing.py
# ---------------------------------------------------------------------------

# Where to point the visible window AND where to re-confirm login. This is the
# CONFIRMED authenticated landing (mainMenu / "Activity Centre"); an
# unauthenticated hit redirects to Delta's real login page automatically.
_FALLBACK_LOGIN_URL = "https://www.delta-esourcing.com/delta/mainMenu.html"
# Left-nav items + header role that only render for an authenticated supplier
# (confirmed live on mainMenu.html). "Resources" is omitted — it can appear in
# public chrome too.
_FALLBACK_LOGGED_IN_MARKER = (
    "a:has-text('Response Manager'), a:has-text('Profile Manager'), "
    "a:has-text('Select Accredit'), a:has-text('Settings'), "
    "header:has-text('Supplier Administrator')"
)
# After login Delta lands the supplier on /delta/mainMenu.html — match any
# authenticated /delta/ page while excluding the login page. NOT /delta/suppliers/.
_FALLBACK_SUCCESS_URL_PATTERN = r"delta-esourcing\.com/delta/(?!login)"


def _load_delta_markers() -> tuple[str, str, str]:
    """Return (login_url, logged_in_marker, success_url_pattern).

    Prefer the backend's canonical constants (so selectors never drift); fall
    back to the inlined mirror when the backend package isn't importable.
    """
    src = Path(__file__).resolve().parent.parent / "tender-agent" / "src"
    if src.is_dir():
        sys.path.insert(0, str(src))
    try:
        from tender_agent.services.portals.adapters.delta_esourcing import (  # noqa: E402
            DELTA_LOGIN_SUCCESS_PATTERN,
            DELTA_SELECTORS,
            DELTA_URLS,
        )

        return (
            DELTA_URLS["main_menu"],
            DELTA_SELECTORS["logged_in_marker"],
            DELTA_LOGIN_SUCCESS_PATTERN,
        )
    except Exception:  # noqa: BLE001 — any import issue → use the mirror.
        return (
            _FALLBACK_LOGIN_URL,
            _FALLBACK_LOGGED_IN_MARKER,
            _FALLBACK_SUCCESS_URL_PATTERN,
        )


DELTA_LOGIN_URL, DELTA_LOGGED_IN_MARKER, DELTA_SUCCESS_URL_PATTERN = (
    _load_delta_markers()
)

# Delta's authenticated session rides on a cookie. Delta eSourcing is a Java
# servlet app, so the session cookie is JSESSIONID; we also accept any other
# *session*-named cookie on the Delta domain so a server-side rename can't
# silently break capture. This is a SAFETY NET to ensure there's actually a
# session cookie to persist — the URL+marker check (mirroring the adapter's
# is_authenticated) is what proves the session is *authenticated*, not anonymous.
# NOTE: a Java app sets JSESSIONID even before login, so cookie presence alone
# is necessary-but-not-sufficient; both gates must pass before we capture.
DELTA_COOKIE_DOMAIN_SUBSTR = "delta-esourcing.com"
DELTA_KNOWN_AUTH_COOKIES = ("JSESSIONID",)


# ---------------------------------------------------------------------------
# Friendly console output (no colour codes — works in any Windows terminal).
# ---------------------------------------------------------------------------


def say(msg: str) -> None:
    print(msg, flush=True)


def step(n: int, total: int, msg: str) -> None:
    print(f"\n[{n}/{total}] {msg}", flush=True)


class ConnectError(Exception):
    """A user-facing failure. Carries a plain-English message + what to do."""


# ---------------------------------------------------------------------------
# Backend HTTP (stdlib only — no `requests` dependency for the operator).
# ---------------------------------------------------------------------------


def make_opener() -> urllib.request.OpenerDirector:
    """An opener with a cookie jar, so the login cookie is reused on upload."""
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def _has_session_cookie(opener: urllib.request.OpenerDirector) -> bool:
    proc = next(
        (
            h
            for h in opener.handlers
            if isinstance(h, urllib.request.HTTPCookieProcessor)
        ),
        None,
    )
    if proc is None:
        return False
    return any(c.name == SESSION_COOKIE_NAME for c in proc.cookiejar)


def backend_login(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    email: str,
    password: str,
) -> None:
    """POST credentials to /accounts/login. On success the session cookie is
    stored in the opener's jar. Raises ConnectError with a plain message on
    bad credentials, network failure, or a missing cookie."""
    url = base_url.rstrip("/") + LOGIN_PATH
    body = json.dumps({"email": email, "password": password}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with opener.open(req, timeout=30):
            pass
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ConnectError(
                "Backend login failed — that email/password was rejected.\n"
                "  → Check your operator email and password and try again."
            ) from exc
        raise ConnectError(
            f"Backend login failed (HTTP {exc.code}).\n"
            f"  → The server responded with an error. Try again shortly; if it "
            f"persists, contact whoever runs the cloud backend."
        ) from exc
    except urllib.error.URLError as exc:
        raise ConnectError(
            f"Couldn't reach the backend at {base_url}.\n"
            f"  → Check your internet connection and that the URL is correct "
            f"(reason: {exc.reason})."
        ) from exc

    if not _has_session_cookie(opener):
        raise ConnectError(
            "Backend login returned no session cookie — cannot authenticate the "
            "upload.\n  → This usually means the backend URL is wrong or it's "
            "not the expected app. Double-check --backend-url."
        )


def upload_session(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    state_path: Path,
) -> dict[str, Any]:
    """POST the storage_state.json contents (raw JSON body) to the upload
    endpoint, reusing the login cookie. Returns the parsed JSON response.
    Raises ConnectError with a plain message on any failure."""
    url = base_url.rstrip("/") + UPLOAD_PATH
    data = state_path.read_bytes()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with opener.open(req, timeout=60) as resp:
            payload = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ConnectError(
                "Upload was rejected — your backend login isn't valid for this "
                "endpoint.\n  → Re-run and make sure you signed in as an "
                "operator account."
            ) from exc
        if exc.code == 400:
            detail = ""
            with contextlib.suppress(Exception):
                detail = json.loads(exc.read().decode("utf-8")).get("detail", "")
            raise ConnectError(
                "The backend rejected the captured session as malformed"
                + (f" ({detail})" if detail else "")
                + ".\n  → Try connecting again; if it keeps happening the login "
                "may not have completed."
            ) from exc
        raise ConnectError(
            f"Upload failed (HTTP {exc.code}).\n"
            f"  → The server errored. Try again shortly."
        ) from exc
    except urllib.error.URLError as exc:
        raise ConnectError(
            f"Upload failed — couldn't reach the backend.\n"
            f"  → Check your connection and try again (reason: {exc.reason})."
        ) from exc

    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return {"ok": True}


# ---------------------------------------------------------------------------
# Headed browser capture (Playwright imported lazily so this module imports
# cleanly even where Playwright isn't installed, e.g. in CI).
# ---------------------------------------------------------------------------


def _is_closed(page: Any) -> bool:
    try:
        return bool(page.is_closed())
    except Exception:  # noqa: BLE001
        return True


def _scopes(page: Any) -> list:
    """The page itself plus all its frames — so a marker living inside an iframe
    is found, not only one in the top document (suspect #2)."""
    scopes = [page]
    try:
        scopes.extend(list(page.frames or []))
    except Exception:  # noqa: BLE001
        pass
    return scopes


def _marker_visible(scope: Any) -> tuple[bool, str | None]:
    """Whether the logged-in marker is visible in this page/frame.

    Checks EVERY match, not just `.first` — Delta's mainMenu has several elements
    matching the OR-selector (e.g. a hidden "Settings" before a visible "Response
    Manager"), and `.first.is_visible()` would read the hidden one and miss the
    real menu. Returns (visible, last_error_repr_or_None); the error is surfaced
    by --debug instead of being silently swallowed (suspect #3).
    """
    try:
        loc = scope.locator(DELTA_LOGGED_IN_MARKER)
        count = loc.count()
    except Exception as exc:  # noqa: BLE001
        return False, repr(exc)
    err: str | None = None
    for i in range(min(count, 20)):
        try:
            if loc.nth(i).is_visible():
                return True, None
        except Exception as exc:  # noqa: BLE001
            err = repr(exc)
    return False, err


def _page_authenticated(page: Any, success_re: re.Pattern[str]) -> tuple[bool, str]:
    """Is THIS page/tab a logged-in Delta session? Same strict criteria as #91 —
    the URL is in the authenticated app area AND not a login page, AND the
    supplier-menu marker is visible (now searched across the page's frames too).
    Returns (ok, a human-readable detail string for --debug)."""
    try:
        url = page.url or ""
    except Exception as exc:  # noqa: BLE001
        return False, f"url-error={exc!r}"
    if "login" in url.lower() or not success_re.search(url):
        return False, f"url={url!r} url_ok=False marker=skipped"
    err: str | None = None
    for scope in _scopes(page):
        visible, scope_err = _marker_visible(scope)
        if visible:
            return True, f"url={url!r} url_ok=True marker=True"
        if scope_err:
            err = scope_err
    return False, (
        f"url={url!r} url_ok=True marker=False" + (f" err={err}" if err else "")
    )


def _looks_authenticated(page: Any, success_re: re.Pattern[str]) -> bool:
    """Boolean form of `_page_authenticated` (kept for callers and tests)."""
    return _page_authenticated(page, success_re)[0]


def _scan_for_authenticated_page(
    pages: list, success_re: re.Pattern[str], debug_sink: list | None = None
) -> Any | None:
    """THE ROOT-CAUSE FIX (suspect #1): scan EVERY open tab, not just the page we
    opened. Delta's login — including the Microsoft SSO hop — can land the
    authenticated mainMenu.html in a different tab/window, so the original page
    object never reaches the logged-in URL. Returns the first authenticated
    page/tab, or None. In --debug mode it records every tab it inspected."""
    candidate = None
    for page in pages:
        if _is_closed(page):
            continue
        ok, detail = _page_authenticated(page, success_re)
        if debug_sink is not None:
            debug_sink.append(f"   tab: {detail}")
        if ok and candidate is None:
            candidate = page
            if debug_sink is None:
                break  # in --debug we keep listing every tab; otherwise stop early
    return candidate


def _confirm_authenticated(
    page: Any, success_re: re.Pattern[str], confirm_wait_s: float = 8.0
) -> bool:
    """Authoritative confirmation — the SAME thing the cloud probe does: navigate
    this page to mainMenu (the exact URL is_authenticated checks), require we're
    not bounced to login, then wait for the supplier marker to render (searched
    in the page AND its frames). Only then is the session genuinely logged in.
    """
    try:
        page.goto(DELTA_LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
    except Exception:  # noqa: BLE001
        return False
    try:
        url = (page.url or "").lower()
    except Exception:  # noqa: BLE001
        return False
    if "login" in url or not success_re.search(url):
        return False
    deadline = time.monotonic() + max(0.0, confirm_wait_s)
    while True:
        for scope in _scopes(page):
            visible, _ = _marker_visible(scope)
            if visible:
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def _delta_session_cookie_present(context: Any) -> bool:
    """True iff the context holds a genuine Delta session cookie to persist.

    Reads cookie NAMES + domains only — values are never inspected or printed.
    Requires a session-named cookie (JSESSIONID, or any `*session*` cookie) on
    the delta-esourcing.com domain, so we never write a cookieless/empty state.
    """
    try:
        cookies = context.cookies()
    except Exception:  # noqa: BLE001
        return False
    for c in cookies:
        domain = str(c.get("domain", "")).lower()
        name = str(c.get("name", ""))
        if (
            DELTA_COOKIE_DOMAIN_SUBSTR in domain
            and c.get("value")
            and (name in DELTA_KNOWN_AUTH_COOKIES or "session" in name.lower())
        ):
            return True
    return False


def capture_storage_state(
    state_path: Path,
    timeout_s: int,
    keep_open: bool,
    debug: bool = False,
) -> None:
    """Open a visible browser at Delta, wait for the human to log in, then
    write the Playwright storage_state to `state_path`. Raises ConnectError on
    timeout, a closed window, or a missing Playwright install. With `debug`,
    prints per-iteration diagnostics (tabs, URLs, marker result, cookie gate)."""
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        raise ConnectError(
            "Playwright isn't installed on this laptop.\n"
            "  → Run these two commands once, then try again:\n"
            "       pip install playwright==1.47.0\n"
            "       playwright install chromium"
        ) from exc

    success_re = re.compile(DELTA_SUCCESS_URL_PATTERN, re.IGNORECASE)

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=False)
        except Exception as exc:  # noqa: BLE001
            raise ConnectError(
                "Couldn't start the Chromium browser.\n"
                "  → Run `playwright install chromium` once, then try again."
            ) from exc

        context = browser.new_context()
        page = context.new_page()

        say("   A browser window is opening — log into Delta as you normally")
        say("   would (including the Microsoft Authenticator prompt on your")
        say("   phone). You do NOT need to type anything back here yet.")

        # A slow first nav is fine; we poll for the logged-in state below.
        with contextlib.suppress(Exception):
            page.goto(DELTA_LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)

        deadline = time.monotonic() + timeout_s
        last_nudge = 0.0
        logged_in = False
        while time.monotonic() < deadline:
            # Scan ALL open tabs (Delta's SSO can land mainMenu in a new one).
            open_pages = [
                pg for pg in (list(getattr(context, "pages", []) or []))
                if not _is_closed(pg)
            ]
            # Only give up as "closed" when EVERY tab is gone — the human may
            # have closed the original tab but completed login in a new one.
            if not open_pages:
                raise ConnectError(
                    "The browser window was closed before login finished.\n"
                    "  → Run the command again and complete the Delta login."
                )
            # Gates, all required, before we trust the session:
            #   1. some open tab is in the authenticated app area (not login)
            #      with the supplier menu visible (searched across tabs+frames);
            #   2. a real Delta session cookie is in the jar;
            #   3. an authoritative re-check on mainMenu (same as the cloud probe).
            debug_sink: list[str] | None = [] if debug else None
            candidate = _scan_for_authenticated_page(open_pages, success_re, debug_sink)
            cookie_ok = _delta_session_cookie_present(context)
            if debug:
                remaining = int(deadline - time.monotonic())
                say(f"[debug] ~{remaining}s left | tabs={len(open_pages)} | "
                    f"cookie_gate={cookie_ok}")
                for line in debug_sink or []:
                    say(line)
            if (
                candidate is not None
                and cookie_ok
                and _confirm_authenticated(candidate, success_re)
            ):
                logged_in = True
                break
            now = time.monotonic()
            if now - last_nudge > 30:
                remaining = int(deadline - now)
                say(f"   ...still waiting for login (about {remaining}s left)")
                last_nudge = now
            time.sleep(2)

        if not logged_in:
            with contextlib.suppress(Exception):
                if not keep_open:
                    browser.close()
            raise ConnectError(
                "Timed out waiting for a fully logged-in Delta session.\n"
                "  → Make sure you finished signing in (and the Authenticator "
                "prompt) so Delta shows your supplier menu, then run the command "
                "again. You can allow more time with --timeout 900."
            )

        say("   Login confirmed (supplier menu + Delta session cookie) —")
        say("   capturing your session...")
        # storage_state(path=...) writes cookies + localStorage to disk.
        context.storage_state(path=str(state_path))

        if not keep_open:
            with contextlib.suppress(Exception):
                browser.close()


# ---------------------------------------------------------------------------
# Temp file handling — the captured state is a credential; never leave it around.
# ---------------------------------------------------------------------------


def new_state_tempfile() -> Path:
    fd, name = tempfile.mkstemp(prefix="delta_session_", suffix=".json")
    os.close(fd)
    return Path(name)


def shred_tempfile(path: Path) -> None:
    """Delete the temp state file. Best-effort; never raises."""
    with contextlib.suppress(Exception):
        if path.exists():
            path.unlink()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="connect_delta",
        description="Capture a Delta login session and upload it to the cloud.",
    )
    parser.add_argument(
        "--backend-url",
        default=os.environ.get("TENDER_BACKEND_URL", DEFAULT_BACKEND_URL),
        help="Cloud backend base URL (default: the deployed UK West app).",
    )
    parser.add_argument(
        "--email",
        default=os.environ.get("TENDER_OPERATOR_EMAIL"),
        help="Operator email for backend login (otherwise you'll be prompted).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_LOGIN_TIMEOUT_S,
        help=f"Seconds to wait for login (default {DEFAULT_LOGIN_TIMEOUT_S}).",
    )
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="Leave the browser open after capture (debugging).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print per-iteration diagnostics (open tabs + their URLs, the "
        "marker-visibility result and any error per tab/frame, and the cookie "
        "gate) — use this if login is never detected.",
    )
    return parser.parse_args(argv)


def prompt_credentials(email: str | None) -> tuple[str, str]:
    if not email:
        email = input("   Operator email: ").strip()
    else:
        say(f"   Operator email: {email}")
    if not email:
        raise ConnectError("No email entered — cannot sign in to the backend.")
    password = getpass.getpass("   Operator password (hidden): ")
    if not password:
        raise ConnectError("No password entered — cannot sign in to the backend.")
    return email, password


def run(args: argparse.Namespace) -> int:
    total = 4
    say("=" * 64)
    say(" Connect Delta — link your Delta login to the cloud backend")
    say("=" * 64)
    say(f" Backend: {args.backend_url}")

    state_path = new_state_tempfile()
    try:
        # 1. Sign in to the backend (so we're allowed to upload).
        step(1, total, "Sign in to the cloud backend")
        email, password = prompt_credentials(args.email)
        opener = make_opener()
        backend_login(opener, args.backend_url, email, password)
        # Drop the password reference as soon as we're done with it.
        del password
        say("   Signed in.")

        # 2. Open a real browser and let the human log into Delta.
        step(2, total, "Log into Delta in the browser that opens")
        capture_storage_state(
            state_path, args.timeout, args.keep_open, debug=args.debug
        )
        say("   Captured!")

        # 3. Upload the captured session to the cloud.
        step(3, total, "Uploading your Delta session to the cloud...")
        result = upload_session(opener, args.backend_url, state_path)
        updated = result.get("updated_at", "")
        say(f"   Uploaded (slug: {result.get('slug', 'delta_esourcing')}"
            + (f", at {updated}" if updated else "") + ").")

        # 4. Done.
        step(4, total, "Done")
        say("   Your Delta session is now in the cloud. The agent can use it")
        say("   for Delta until the session expires — re-run this when Delta")
        say("   asks you to log in again.")
        return 0
    except ConnectError as exc:
        say("\n" + "!" * 64)
        say(" Couldn't finish.")
        say("!" * 64)
        say(str(exc))
        return 1
    except KeyboardInterrupt:
        say("\nCancelled.")
        return 130
    finally:
        # The captured session is a credential — never leave it on disk.
        shred_tempfile(state_path)


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
