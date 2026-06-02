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

# Where to point the visible window. Delta redirects an unauthenticated user
# from the Response Manager to its real login page automatically.
_FALLBACK_LOGIN_URL = (
    "https://www.delta-esourcing.com/delta/suppliers/select/addToList.html"
)
# A left-menu item that only renders for an authenticated supplier.
_FALLBACK_LOGGED_IN_MARKER = (
    "a:has-text('Response Manager'), a:has-text('Profile Manager'), "
    "a:has-text('Select Accredit'), #supplierMenu, nav a:has-text('Resources')"
)
# After login Delta lands the supplier under /delta/suppliers/.
_FALLBACK_SUCCESS_URL_PATTERN = r"delta-esourcing\.com/delta/suppliers/"


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
            DELTA_URLS["response_manager"],
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


def _is_logged_in(page: Any, success_re: re.Pattern[str]) -> bool:
    """True once the supplier menu marker is visible, or the URL has landed in
    the authenticated supplier area. Mirrors the adapter's is_authenticated."""
    with contextlib.suppress(Exception):
        if page.locator(DELTA_LOGGED_IN_MARKER).first.is_visible(timeout=500):
            return True
    with contextlib.suppress(Exception):
        url = page.url or ""
        if success_re.search(url) and "login" not in url.lower():
            return True
    return False


def capture_storage_state(
    state_path: Path,
    timeout_s: int,
    keep_open: bool,
) -> None:
    """Open a visible browser at Delta, wait for the human to log in, then
    write the Playwright storage_state to `state_path`. Raises ConnectError on
    timeout, a closed window, or a missing Playwright install."""
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
            # If the operator closed the window, stop with a clear message.
            if page.is_closed():
                raise ConnectError(
                    "The browser window was closed before login finished.\n"
                    "  → Run the command again and complete the Delta login."
                )
            if _is_logged_in(page, success_re):
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
                "Timed out waiting for login — I didn't see a logged-in Delta "
                "session.\n"
                "  → Make sure you finished signing in (and the Authenticator "
                "prompt) in the browser window, then run the command again. "
                "You can allow more time with --timeout 900."
            )

        say("   Login detected — capturing your session...")
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
        capture_storage_state(state_path, args.timeout, args.keep_open)
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
