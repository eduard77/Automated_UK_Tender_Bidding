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
    # Accept action — matched by VISIBLE TEXT via Playwright's text engine
    # (`>> text=`), NOT by class/tag, because the live diagnostic found the
    # dialog container but NOT a `button:has-text('Accept all')` — the real
    # control's markup (anchor? input? span?) is unknown. The text engine
    # matches whatever element carries the text. This selector is what the
    # diagnostic reports under `cookie_accept`; the FLOW dismisses via
    # `click_by_text` (same text), so the two stay aligned.
    "cookie_accept": (
        ".js-cookie-consent-dialog >> text=/accept all/i, "
        ".cookie-dialog >> text=/accept all/i, "
        "[role='dialog'][aria-labelledby*='cookie' i] >> text=/accept all/i"
    ),
}

#: Text variants tried (in order) to accept the consent dialog.
COOKIE_ACCEPT_TEXTS = ("Accept all", "Accept", "I agree", "Agree")

#: JS that detaches every known cookie-overlay container — strategy 3, used
#: only when the text-accept didn't clear the dialog. Returns the count
#: removed. CSS Level-4 case-insensitive attribute match (`i`) is supported
#: by Chromium's querySelectorAll.
_REMOVE_OVERLAY_JS = """
() => {
  const sels = [
    '.js-cookie-consent-dialog',
    '.cookie-dialog',
    "[role='dialog'][aria-labelledby*='cookie' i]"
  ];
  let removed = 0;
  for (const s of sels) {
    document.querySelectorAll(s).forEach(el => { el.remove(); removed++; });
  }
  return removed;
}
"""

#: JS that submits the login form programmatically — strategy 5's deepest
#: fallback when even a forced click can't reach the button.
_SUBMIT_FORM_JS = """
() => {
  const f = document.querySelector('form#loginForm, form[action*="Login" i], form');
  if (!f) return false;
  if (f.requestSubmit) { f.requestSubmit(); } else { f.submit(); }
  return true;
}
"""

#: How long to wait for the cookie dialog to detach after a dismissal action
#: before moving to the next strategy.
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
    #: WHICH strategy got us past the consent overlay, for at-a-glance
    #: regression triage: "text-accept" (clicked Accept-all by text),
    #: "removed" (detached the overlay via JS), "forced-submit" (overlay
    #: stayed but we forced the login submit through), or "none" (no dialog,
    #: or nothing was needed).
    cookie_dismiss_method: str | None = None


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
    cookie_dismiss_method: str | None = None
    try:
        dialog_present = await bridge.element_exists(
            slug, PROACTIS_LOGIN_SELECTORS["cookie_dialog"]
        )
    except BridgeError:
        dialog_present = False
    if dialog_present:
        cookie_dialog_dismissed, cookie_dismiss_method = (
            await _dismiss_cookie_dialog(bridge, slug)
        )

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
            cookie_dismiss_method=cookie_dismiss_method,
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
            cookie_dismiss_method=cookie_dismiss_method,
        )

    # 3. Submit. If a residual overlay still intercepts the click, fall back
    # to a forced click and then a programmatic form submit (strategy 5) so a
    # stray dialog can't block authentication.
    try:
        await bridge.click(slug, PROACTIS_LOGIN_SELECTORS["submit_button"])
    except BridgeError as exc:
        forced = await _force_submit(bridge, slug)
        if not forced:
            return LoginAttempt(
                status="needs_login",
                detail=(
                    f"submit click intercepted and force-submit failed: {exc}"
                ),
                cookie_dialog_dismissed=cookie_dialog_dismissed,
                cookie_dismiss_method=cookie_dismiss_method,
            )
        # We got the form submitted past the overlay — record that as the
        # decisive action so a regression is obvious in the diagnostic.
        cookie_dismiss_method = "forced-submit"

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
            cookie_dismiss_method=cookie_dismiss_method,
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
            cookie_dismiss_method=cookie_dismiss_method,
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
            cookie_dismiss_method=cookie_dismiss_method,
        )
    return LoginAttempt(
        status="needs_login",
        detail="Form submitted but the post-login marker did not appear.",
        current_url=status.get("current_url"),
        cookie_dialog_dismissed=cookie_dialog_dismissed,
        cookie_dismiss_method=cookie_dismiss_method,
    )


async def _dialog_gone(bridge: BridgeClient, slug: str) -> bool:
    """Poll until the cookie-dialog container is no longer in the DOM, up to
    the dismiss timeout. True once it's gone, False if it outlasts the wait."""
    deadline = asyncio.get_event_loop().time() + COOKIE_DIALOG_DISMISS_TIMEOUT_S
    while True:
        try:
            still_visible = await bridge.element_exists(
                slug, PROACTIS_LOGIN_SELECTORS["cookie_dialog"]
            )
        except BridgeError:
            still_visible = False
        if not still_visible:
            return True
        if asyncio.get_event_loop().time() >= deadline:
            return False
        await asyncio.sleep(COOKIE_DIALOG_POLL_INTERVAL_S)


async def _dismiss_cookie_dialog(
    bridge: BridgeClient, slug: str
) -> tuple[bool, str]:
    """Clear the consent overlay using progressively more forceful strategies,
    stopping as soon as the dialog container is gone. Returns
    `(dismissed, method)` where method ∈ {"text-accept", "removed", "none"}.

    Strategy 1 — click "Accept all" (then "Accept", …) by VISIBLE TEXT scoped
    to the dialog, via the bridge's `click_by_text` (Playwright text/role
    engine). This is markup-agnostic: it doesn't matter whether the control is
    a button, anchor, input or span. The live diagnostic found the dialog
    container but NOT a class-based accept button, so text is the robust path.

    Strategy 3 — if the text accept didn't clear it (or the bridge lacks
    `click_by_text`), DETACH the overlay nodes with a small JS snippet via the
    bridge's `evaluate`. The dialog only matters because it overlays the form;
    removing it stops the interception.

    Every step is best-effort: a missing optional bridge method or a
    BridgeError just falls through to the next strategy. (Strategy 5, the
    forced/programmatic submit, lives at the submit step in
    `login_with_credentials` — by then we know whether a click is still
    blocked.)"""
    click_by_text = getattr(bridge, "click_by_text", None)
    if click_by_text is not None:
        for text in COOKIE_ACCEPT_TEXTS:
            try:
                clicked = await click_by_text(
                    slug, PROACTIS_LOGIN_SELECTORS["cookie_dialog"], text
                )
            except BridgeError:
                clicked = False
            if clicked and await _dialog_gone(bridge, slug):
                logger.info(
                    "discovery.proactis.cookie_dialog_dismissed",
                    slug=slug,
                    method="text-accept",
                    text=text,
                )
                return True, "text-accept"

    evaluate = getattr(bridge, "evaluate", None)
    if evaluate is not None:
        try:
            await evaluate(slug, _REMOVE_OVERLAY_JS)
        except BridgeError:
            pass
        else:
            if await _dialog_gone(bridge, slug):
                logger.info(
                    "discovery.proactis.cookie_dialog_dismissed",
                    slug=slug,
                    method="removed",
                )
                return True, "removed"

    logger.info(
        "discovery.proactis.cookie_dialog_still_visible",
        slug=slug,
        timeout_s=COOKIE_DIALOG_DISMISS_TIMEOUT_S,
    )
    return False, "none"


async def _force_submit(bridge: BridgeClient, slug: str) -> bool:
    """Strategy 5: the normal submit click was intercepted. Try a FORCED click
    on the submit button (ignores the intercepting overlay), then a
    programmatic `form.submit()` via JS. Returns True if either fired."""
    force_click = getattr(bridge, "force_click", None)
    if force_click is not None:
        try:
            await force_click(slug, PROACTIS_LOGIN_SELECTORS["submit_button"])
            logger.info("discovery.proactis.submit_forced_click", slug=slug)
            return True
        except BridgeError:
            pass

    evaluate = getattr(bridge, "evaluate", None)
    if evaluate is not None:
        try:
            submitted = await evaluate(slug, _SUBMIT_FORM_JS)
        except BridgeError:
            submitted = False
        if submitted:
            logger.info("discovery.proactis.submit_form_js", slug=slug)
            return True
    return False
