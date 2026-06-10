"""Proactis login-failure diagnostics — capture WHAT the browser actually hit.

A real cloud run produced `discovery.proactis.login_blocked` with
`current_url=null` and nothing else to go on: the failure could equally be a
datacenter/bot block (like Delta's 403), a cookie/consent interstitial, a
JS-rendered form the selectors missed, a redirect loop, or a plain timeout.
This module reads the browser state AT the failure moment and logs/returns a
non-secret snapshot so the operator can tell those apart:

* the real current_url (with an explicit flag when even that is unreadable),
* page title + the HTTP status of the last navigation (when the bridge has it),
* a trimmed, tag-free slice of the page text (~2000 chars — enough to
  recognise "Access denied", a Cloudflare challenge, a cookie wall, or the
  login form itself),
* which of the login-flow selectors are actually present/visible on the page
  (per-frame when the bridge supports the frame-aware probe),
* the frames on the page (some portals render login inside an iframe),
* a best-effort screenshot path.

Hard rule: NO secrets. Never the password (only the existing fingerprint the
login flow already logs), never cookie values. The page-text slice comes from
`inner_text` — form input VALUES are not text nodes, so the typed email /
password can't leak through it.

This module deliberately does not import `proactis_discovery` (which imports
it) — the bridge slug is passed in by callers.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

import structlog

from tender_agent.services.bridge_client import BridgeClient
from tender_agent.services.discovery.proactis_login import (
    PROACTIS_LOGIN_SELECTORS,
    LoginAttempt,
    login_with_credentials,
)
from tender_agent.services.portals.adapters.proactis import (
    PROACTIS_SELECTORS,
    PROACTIS_URLS,
)

logger = structlog.get_logger(__name__)

#: Page-text slice size. Big enough to recognise a block page / cookie wall /
#: login form; small enough to keep one log event readable.
TEXT_EXCERPT_CHARS = 2000

#: Selector inventory the snapshot reports on: every control the login flow
#: tries to drive, plus the post-login success marker. Labels are stable keys
#: the operator can grep for.
_DIAGNOSTIC_SELECTORS: dict[str, str] = {
    "email_input": PROACTIS_LOGIN_SELECTORS["email_input"],
    "password_input": PROACTIS_LOGIN_SELECTORS["password_input"],
    "submit_button": PROACTIS_LOGIN_SELECTORS["submit_button"],
    "invalid_credentials_banner": PROACTIS_LOGIN_SELECTORS[
        "invalid_credentials_banner"
    ],
    "logged_in_marker": PROACTIS_SELECTORS["logged_in_marker"],
}


@dataclass
class SelectorPresence:
    """Whether one login-flow selector matched anything on the failed page."""

    found: bool = False
    visible: bool = False
    frame: str | None = None
    error: str | None = None


@dataclass
class LoginDiagnostic:
    """Non-secret snapshot of the browser at the login-failure moment."""

    logged_in: bool
    outcome: str  # LoginAttempt.status ("ok" included for the admin probe)
    detail: str | None
    current_url: str | None
    #: True when even the URL could not be read — the exact `current_url=null`
    #: condition from the live log, surfaced explicitly instead of silently.
    current_url_unreadable: bool
    page_title: str | None
    last_status_code: int | None
    selectors_found: dict[str, SelectorPresence] = field(default_factory=dict)
    frames: list[str] = field(default_factory=list)
    page_text_excerpt: str = ""
    screenshot_path: str | None = None
    capture_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _collapse_text(text: str) -> str:
    """Whitespace-collapse the page text and trim to the excerpt budget."""
    return re.sub(r"\s+", " ", text or "").strip()[:TEXT_EXCERPT_CHARS]


async def capture_login_state(
    bridge: BridgeClient,
    slug: str,
    *,
    attempt: LoginAttempt,
) -> LoginDiagnostic:
    """Read the browser state for an already-finished login attempt and log it
    under `discovery.proactis.login_diagnostic`. Best-effort throughout: any
    individual read failing is recorded in the snapshot, never raised — this
    runs on an already-failing path and must not mask the original outcome."""
    current_url: str | None = None
    last_status_code: int | None = None
    page_title: str | None = None
    frames: list[str] = []
    selectors: dict[str, SelectorPresence] = {}
    excerpt = ""
    screenshot_path: str | None = None
    capture_errors: list[str] = []

    try:
        status = await bridge.session_status(slug)
        current_url = status.get("current_url")
        last_status_code = status.get("last_status_code")
    except Exception as exc:  # noqa: BLE001
        capture_errors.append(f"session_status: {exc}")

    # Selector presence — frame-aware when the bridge has the rich probe
    # (in-process client), top-frame element_exists otherwise (HTTP client).
    probe = getattr(bridge, "probe_login_markers", None)
    if probe is not None:
        try:
            result = await probe(
                slug,
                [
                    {"label": label, "selector": selector}
                    for label, selector in _DIAGNOSTIC_SELECTORS.items()
                ],
                timeout_ms=1000,
            )
            page_title = result.page_title
            current_url = current_url or result.current_url
            frames = list(result.frames_searched)
            for alt in result.alternatives:
                selectors[alt.label] = SelectorPresence(
                    found=alt.found,
                    visible=alt.visible,
                    frame=alt.frame,
                    error=alt.error,
                )
        except Exception as exc:  # noqa: BLE001
            capture_errors.append(f"probe_login_markers: {exc}")
    else:
        for label, selector in _DIAGNOSTIC_SELECTORS.items():
            try:
                found = await bridge.element_exists(slug, selector)
                selectors[label] = SelectorPresence(found=found, visible=found)
            except Exception as exc:  # noqa: BLE001
                selectors[label] = SelectorPresence(error=str(exc))

    try:
        excerpt = _collapse_text(await bridge.page_text(slug))
    except Exception as exc:  # noqa: BLE001
        capture_errors.append(f"page_text: {exc}")

    try:
        shot = await bridge.screenshot(slug, label=f"{slug}-login-diagnostic")
        screenshot_path = shot.get("path")
    except Exception as exc:  # noqa: BLE001
        capture_errors.append(f"screenshot: {exc}")

    diagnostic = LoginDiagnostic(
        logged_in=attempt.status == "ok",
        outcome=attempt.status,
        detail=attempt.detail,
        current_url=current_url,
        current_url_unreadable=not current_url,
        page_title=page_title,
        last_status_code=last_status_code,
        selectors_found=selectors,
        frames=frames,
        page_text_excerpt=excerpt,
        screenshot_path=screenshot_path,
        capture_error="; ".join(capture_errors) or None,
    )
    # The whole point: one info event carrying everything needed to tell a
    # block page from a cookie wall from a missing form. No secrets — see the
    # module docstring for why the text excerpt can't contain the password.
    logger.info("discovery.proactis.login_diagnostic", **diagnostic.as_dict())
    return diagnostic


async def run_login_diagnostic(
    *,
    credentials: Any,  # services.portals.base.Credentials
    slug: str,
    bridge: BridgeClient | None = None,
) -> dict[str, Any]:
    """One-shot, READ-ONLY probe for the admin endpoint: open a bridge
    session, attempt the stored-credential login, capture the snapshot, close
    the session. Logs in + reads the page — never registers interest, never
    clicks anything beyond the login submit, never changes portal state.

    Returns a JSON-ready dict mirroring `delta_session.test_session`'s shape:
    `available`/`logged_in` always present, the diagnostic fields added."""
    from tender_agent.services.bridge_client import make_bridge_client

    bridge = bridge if bridge is not None else make_bridge_client()

    if not await bridge.bridge_available():
        return {
            "available": False,
            "logged_in": False,
            "detail": "browser bridge / Playwright not available",
        }

    try:
        await bridge.open_session(slug, PROACTIS_URLS["login"])
    except Exception as exc:  # noqa: BLE001
        return {
            "available": True,
            "logged_in": False,
            "detail": f"open_session failed: {exc}",
        }

    try:
        attempt = await login_with_credentials(
            bridge, slug=slug, credentials=credentials
        )
        # Capture on success too — the admin probe exists to SHOW the page,
        # whichever way the attempt went.
        diagnostic = await capture_login_state(bridge, slug, attempt=attempt)
        return {
            "available": True,
            "logged_in": diagnostic.logged_in,
            **diagnostic.as_dict(),
        }
    finally:
        try:
            await bridge.close_session(slug)
        except Exception:  # noqa: BLE001
            logger.debug("discovery.proactis.diagnostic_close_failed")
