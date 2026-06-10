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

import asyncio
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
    # Cookie/consent DIALOG container. The previous live diagnostic
    # (2026-06-10) showed Proactis renders the consent as a modal dialog with
    # this stable class — it sits on top of the form and intercepted the
    # submit click 42 times before timeout. We probe for the CONTAINER (not
    # the button) to decide whether to attempt a dismiss at all.
    "cookie_dialog": (
        ".js-cookie-consent-dialog.is-visible, "
        ".js-cookie-consent-dialog, "
        ".cookie-dialog[role='dialog'], "
        "[role='dialog'][aria-labelledby*='cookie' i]"
    ),
    # Accept button — SCOPED to the dialog so we never click an unrelated
    # "Accept" elsewhere on the page. Fallbacks broaden the text and lift the
    # scope only as a last resort.
    "cookie_accept": (
        ".js-cookie-consent-dialog button:has-text('Accept all'), "
        ".js-cookie-consent-dialog a:has-text('Accept all'), "
        ".cookie-dialog button:has-text('Accept all'), "
        "[role='dialog'][aria-labelledby*='cookie' i] button:has-text('Accept all'), "
        ".js-cookie-consent-dialog button:has-text('Accept'), "
        "button:has-text('Accept all')"
    ),
}


#: How long to wait for the cookie dialog to detach after the accept click
#: before we give up and let the form-click's own auto-wait deal with it.
COOKIE_DIALOG_DISMISS_TIMEOUT_S = 5.0
COOKIE_DIALOG_POLL_INTERVAL_S = 0.1


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
    #: Tri-state telemetry for the cookie-dialog dismissal that runs before
    #: the form fill. None = no attempt made (the flow exited before reaching
    #: that step, or the dialog wasn't present); True = dismissed and
    #: confirmed detached within the deadline; False = found but failed to
    #: clear (the form click may still succeed if the dialog stops
    #: intercepting pointer events, but the diagnostic gets to see this).
    cookie_dialog_dismissed: bool | None = None


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

    # 1b. Dismiss the cookie/consent dialog if one is up — it's a real modal
    # (`role=dialog .js-cookie-consent-dialog.is-visible`) that the live
    # diagnostic showed intercepting the submit click 42× before timeout.
    # Detect the dialog container first; only attempt the dismiss when one is
    # actually present. Telemetry goes onto the LoginAttempt so the diagnostic
    # snapshot can report `cookie_dialog_dismissed` truthfully.
    cookie_dialog_dismissed: bool | None = None
    try:
        dialog_present = await bridge.element_exists(
            slug, PROACTIS_LOGIN_SELECTORS["cookie_dialog"]
        )
    except BridgeError:
        dialog_present = False
    if dialog_present:
        cookie_dialog_dismissed = await _dismiss_cookie_dialog(bridge, slug)

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
            cookie_dialog_dismissed=cookie_dialog_dismissed,
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
            cookie_dialog_dismissed=cookie_dialog_dismissed,
        )

    # 3. Submit.
    try:
        await bridge.click(slug, PROACTIS_LOGIN_SELECTORS["submit_button"])
    except BridgeError as exc:
        return LoginAttempt(
            status="needs_login",
            detail=f"submit button not found on /Login/Index: {exc}",
            cookie_dialog_dismissed=cookie_dialog_dismissed,
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
            cookie_dialog_dismissed=cookie_dialog_dismissed,
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
            cookie_dialog_dismissed=cookie_dialog_dismissed,
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
            cookie_dialog_dismissed=cookie_dialog_dismissed,
        )
    return LoginAttempt(
        status="needs_login",
        detail="Form submitted but the post-login marker did not appear.",
        current_url=status.get("current_url"),
        cookie_dialog_dismissed=cookie_dialog_dismissed,
    )


async def _dismiss_cookie_dialog(bridge: BridgeClient, slug: str) -> bool:
    """Click the cookie dialog's Accept-all button, then poll until the
    dialog container detaches. Returns True only on confirmed dismissal —
    so the diagnostic can show `cookie_dialog_dismissed: true` honestly.

    Defensive: any BridgeError on the accept click yields False; the form
    fill still goes ahead because the submit click's own auto-wait may yet
    clear the overlay (or report it again in the next diagnostic)."""
    try:
        await bridge.click(slug, PROACTIS_LOGIN_SELECTORS["cookie_accept"])
    except BridgeError as exc:
        logger.info(
            "discovery.proactis.cookie_dialog_accept_failed",
            slug=slug,
            error=str(exc),
        )
        return False
    deadline = asyncio.get_event_loop().time() + COOKIE_DIALOG_DISMISS_TIMEOUT_S
    while asyncio.get_event_loop().time() < deadline:
        try:
            still_visible = await bridge.element_exists(
                slug, PROACTIS_LOGIN_SELECTORS["cookie_dialog"]
            )
        except BridgeError:
            still_visible = False
        if not still_visible:
            logger.info("discovery.proactis.cookie_dialog_dismissed", slug=slug)
            return True
        await asyncio.sleep(COOKIE_DIALOG_POLL_INTERVAL_S)
    logger.info(
        "discovery.proactis.cookie_dialog_still_visible",
        slug=slug,
        timeout_s=COOKIE_DIALOG_DISMISS_TIMEOUT_S,
    )
    return False
