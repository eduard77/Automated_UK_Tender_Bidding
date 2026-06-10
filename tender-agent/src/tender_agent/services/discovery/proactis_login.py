"""Proactis (procontract.due-north.com) supplier login by stored credentials.

The existing `proactis_discovery._ensure_authenticated` only waits for a human
to log in at the visible bridge window. The cloud-headless backend has no
human — it must fill the login form itself from the encrypted credentials
store (the SAME store Delta uses) and detect success.

Hard rules
----------
* Credentials NEVER appear in logs, return values, or error messages. The
  ONLY place the password lives unredacted is in the encrypted blob the
  credentials store wrote, and the local in-memory string we pass to
  `bridge.fill`. Logs carry a fingerprint (first byte + length) at most.
* If the login form's selectors don't match, we surface a CLEAR diagnostic
  (`status="needs_login"`, `detail` naming the missing control) rather than
  spinning until timeout.
* This module is for SEARCH. Document-fetch (Express Interest, downloads)
  is human-gated and lives in the portal adapter; nothing here touches it.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

import structlog

from tender_agent.services.bridge_client import BridgeClient, BridgeError
from tender_agent.services.portals.adapters.proactis import (
    PROACTIS_SELECTORS,
    PROACTIS_URLS,
)
from tender_agent.services.portals.base import Credentials

logger = structlog.get_logger(__name__)


# Selectors used to drive the login form. Grounded in the LIVE login-diagnostic
# capture of /Login/Index (2026-06-10, ProContract V3): page_title "Log In",
# HTTP 200, page text "Welcome to ProContract … User Name … Password …
# Continue", plus a cookie banner ("Accept all"). The original email-shaped
# username selector never matched (Proactis has NO email field) — the fill
# timed out and every run ended needs_login. The password selector matched
# (found, visible, main frame) and is unchanged.
#
# This dict is the SINGLE source for both the login flow and the
# login-diagnostic's `selectors_found` report — keep them from drifting apart.
PROACTIS_LOGIN_SELECTORS = {
    # The "User Name" field. ASP.NET MVC convention is name/id "UserName";
    # attribute-contains + case-insensitive keeps a minor markup difference
    # matching. Email-shaped fallbacks stay at the END for tenant variants
    # that do use an email login — they match nothing on ProContract V3.
    "username_input": (
        "input[name*='UserName' i], input[id*='UserName' i], "
        "input[name*='User' i][type='text'], input[id*='User' i][type='text'], "
        "input[aria-label*='User' i], input[autocomplete='username'], "
        "input[type='email'], input[name*='Email' i]"
    ),
    # Password field — VERIFIED matching by the live diagnostic; unchanged.
    "password_input": (
        "input[type='password'], input[name*='Password' i], "
        "input[id*='Password' i]"
    ),
    # The login button is labelled "Continue" on ProContract V3 (per the
    # captured page text); keep Log In variants + a bare submit fallback.
    "submit_button": (
        "button[type='submit']:has-text('Continue'), "
        "input[type='submit'][value*='Continue' i], "
        "button:has-text('Continue'), "
        "button[type='submit']:has-text('Log in'), "
        "input[type='submit'][value*='Log' i], "
        "button[type='submit']"
    ),
    # An in-page error banner Proactis shows on bad creds. If we find this
    # AFTER submit, we know the credentials were rejected; the generic
    # "still on /Login" check would otherwise miss this case if Proactis
    # returns 200 with the same URL.
    "invalid_credentials_banner": (
        ".validation-summary-errors, .alert-danger, "
        ":has-text('Invalid username or password')"
    ),
    # Cookie/consent banner accept ("Accept all" per the captured page text).
    # Dismissed defensively BEFORE the form fill in case it overlays the
    # controls; its absence is never an error.
    "cookie_accept": (
        "button:has-text('Accept all'), a:has-text('Accept all')"
    ),
}


LoginStatus = Literal["ok", "needs_login", "credentials_rejected", "error"]


@dataclass
class LoginAttempt:
    """Outcome of one `login_with_credentials` call.

    `status="ok"` is the only path the discovery run continues from. Every
    other value short-circuits to a `needs_login`-shaped DiscoveryRunResult
    so the operator sees a clean diagnostic instead of an endless probe.
    """

    status: LoginStatus
    detail: str | None = None
    current_url: str | None = None


def _password_fingerprint(password: str) -> str:
    """Return a short, NON-REVERSIBLE marker for the password so structured
    logs can confirm WHICH credential blob we just used without ever logging
    the plaintext. Hash truncated to 8 hex chars."""
    if not password:
        return "empty"
    return hashlib.sha256(password.encode("utf-8")).hexdigest()[:8]


def _is_login_url(url: str | None) -> bool:
    return bool(url) and "/Login" in url  # type: ignore[arg-type]


async def login_with_credentials(
    bridge: BridgeClient,
    *,
    slug: str,
    credentials: Credentials,
) -> LoginAttempt:
    """Drive Proactis's `/Login/Index` form with the supplied credentials and
    detect success.

    Caller already opened the bridge session for `slug`. We:
      1. Navigate to /Login/Index.
      2. Fill the email + password inputs.
      3. Click submit.
      4. Wait for either (a) the post-login home selector, OR (b) the
         invalid-credentials banner, whichever appears first.

    Returns a LoginAttempt; never raises. The discovery service maps a non-ok
    LoginAttempt into the existing `needs_login` DiscoveryRunResult.
    """
    if not credentials.email and not credentials.username:
        return LoginAttempt(
            status="error", detail="no email/username on credentials record"
        )
    if not credentials.password:
        return LoginAttempt(status="error", detail="no password on credentials record")

    # ProContract V3's field is "User Name", so the stored username comes
    # first; the email is the fallback (and for this operator they're the
    # same address anyway).
    username = credentials.username or credentials.email or ""
    fp = _password_fingerprint(credentials.password)
    email_domain = username.split("@")[-1] if "@" in username else None
    logger.info(
        "discovery.proactis.login_attempt",
        slug=slug,
        email_domain=email_domain,
        pwd_fp=fp,
    )

    # 1. Navigate to the form.
    try:
        await bridge.navigate(slug, PROACTIS_URLS["login"])
    except BridgeError as exc:
        return LoginAttempt(status="error", detail=f"navigate /Login failed: {exc}")

    # 1b. Dismiss the cookie/consent banner if one is up, in case it overlays
    # the form controls. The live diagnostic showed the password field was
    # already visible WITH the banner present, so this is belt-and-braces:
    # accept when present, silently move on when absent or unclickable.
    try:
        if await bridge.element_exists(
            slug, PROACTIS_LOGIN_SELECTORS["cookie_accept"]
        ):
            await bridge.click(slug, PROACTIS_LOGIN_SELECTORS["cookie_accept"])
            logger.info("discovery.proactis.cookie_banner_accepted", slug=slug)
    except BridgeError:
        logger.debug("discovery.proactis.cookie_banner_skip", slug=slug)

    # 2. Fill credentials. We fill THEN check element_exists because some
    # Proactis variants render the form behind a disclosure; in that case the
    # username field isn't immediately present.
    try:
        await bridge.fill(
            slug, PROACTIS_LOGIN_SELECTORS["username_input"], username
        )
    except BridgeError as exc:
        return LoginAttempt(
            status="needs_login",
            detail=(
                f"username input not found on /Login/Index (selector mismatch) "
                f"— first run after a Proactis redesign? Error: {exc}"
            ),
        )
    try:
        await bridge.fill(
            slug,
            PROACTIS_LOGIN_SELECTORS["password_input"],
            credentials.password,
        )
    except BridgeError as exc:
        return LoginAttempt(
            status="needs_login",
            detail=f"password input not found on /Login/Index: {exc}",
        )

    # 3. Submit.
    try:
        await bridge.click(slug, PROACTIS_LOGIN_SELECTORS["submit_button"])
    except BridgeError as exc:
        return LoginAttempt(
            status="needs_login",
            detail=f"submit button not found on /Login/Index: {exc}",
        )

    # 4. Did we land on an authenticated page, OR did Proactis show an
    # invalid-credentials banner? Try the success marker first; only if it
    # misses do we check for the rejection banner.
    try:
        marker_visible = await bridge.element_exists(
            slug, PROACTIS_SELECTORS["logged_in_marker"]
        )
    except BridgeError:
        marker_visible = False
    if marker_visible:
        status = await bridge.session_status(slug)
        return LoginAttempt(
            status="ok",
            detail=f"pwd_fp={fp}",
            current_url=status.get("current_url"),
        )

    # Probe for the rejection banner. If it's visible, the credentials were
    # syntactically accepted but rejected — that's NOT a "needs login"; it's
    # a stored-credential-stale signal. Surface as a distinct status so the
    # operator updates the credential, not the captured session.
    try:
        rejected = await bridge.element_exists(
            slug, PROACTIS_LOGIN_SELECTORS["invalid_credentials_banner"]
        )
    except BridgeError:
        rejected = False
    if rejected:
        return LoginAttempt(
            status="credentials_rejected",
            detail=(
                "Proactis displayed an invalid-credentials banner after "
                "submit. Update the stored Proactis credentials."
            ),
        )

    # Still on /Login but no banner — most likely a captcha or a multi-step
    # login. Surface as needs_login with a hint.
    status = await bridge.session_status(slug)
    if _is_login_url(status.get("current_url")):
        return LoginAttempt(
            status="needs_login",
            detail=(
                "Form submitted but still on /Login (no marker, no banner). "
                "Likely a captcha or a multi-step login step."
            ),
            current_url=status.get("current_url"),
        )
    return LoginAttempt(
        status="needs_login",
        detail="Form submitted but the post-login marker did not appear.",
        current_url=status.get("current_url"),
    )
