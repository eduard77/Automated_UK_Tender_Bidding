"""Login model B (Delta cloud stage 3) — operator-supplied Delta session.

The operator logs into Delta in their OWN browser, exports the authenticated
session as a Playwright `storage_state.json`, and uploads it. We validate it and
write it into the `delta_esourcing` slug's persistent-context dir; the existing
in-process headless fetch re-applies it on launch (see
`bridge_in_process._apply_storage_state`). The subsequent fetch is then
authenticated.

What this is NOT: no password is stored, no login is automated, no browser is
streamed. It is purely "here is a session I already established; reuse it." The
Delta adapter selectors, the orchestrator state machine, and the
`needs_user_confirmation` human-pause are untouched — this is a new front door
that drops auth state into the directory the fetch already reads.

Security: the uploaded storage_state is a CREDENTIAL. It is written only to the
bridge_state_dir target; its cookie/localStorage VALUES are never logged or
returned by any endpoint (status reports counts + a coarse heuristic only). The
three admin endpoints are gated behind `require_account` (operator-only).
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
from datetime import UTC, datetime
from typing import Any

import structlog

from tender_agent.services.bridge_in_process import storage_state_path
from tender_agent.services.portals.adapters.delta_esourcing import (
    DeltaEsourcingAdapter,
)
from tender_agent.services.portals.base import PortalContext

logger = structlog.get_logger(__name__)

# The slug whose persistent-context dir the Delta fetch loads. Anchored on the
# adapter so it can never drift from the dir the orchestrator actually uses.
DELTA_SLUG = DeltaEsourcingAdapter.platform_slug or "delta_esourcing"

# A real storage_state is a few KB; reject anything absurd before parsing.
MAX_STORAGE_STATE_BYTES = 2 * 1024 * 1024  # 2 MB


class StorageStateValidationError(ValueError):
    """Raised when an uploaded payload isn't a well-formed Playwright
    storage_state. The API maps this to HTTP 400."""


def validate_storage_state(raw: bytes | str) -> dict[str, Any]:
    """Parse + structurally validate a Playwright storage_state payload.

    Well-formed = a JSON object with a `cookies` list and an `origins` list
    (Playwright always exports both; either may be empty). Each cookie must be
    an object carrying a string name, a value, and a domain (or a url). Each
    origin must be an object with a string `origin`.

    We do NOT assert the session is actually *logged in* — that's what
    `test_session` is for. Raises StorageStateValidationError on any problem.
    """
    if isinstance(raw, bytes):
        if len(raw) > MAX_STORAGE_STATE_BYTES:
            raise StorageStateValidationError("storage_state is too large")
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StorageStateValidationError(
                "storage_state is not valid UTF-8"
            ) from exc
    if len(raw) > MAX_STORAGE_STATE_BYTES:
        raise StorageStateValidationError("storage_state is too large")

    try:
        state = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StorageStateValidationError(f"not valid JSON: {exc}") from exc

    if not isinstance(state, dict):
        raise StorageStateValidationError("storage_state must be a JSON object")

    cookies = state.get("cookies")
    origins = state.get("origins")
    if not isinstance(cookies, list):
        raise StorageStateValidationError("storage_state.cookies must be a list")
    if not isinstance(origins, list):
        raise StorageStateValidationError("storage_state.origins must be a list")

    for i, c in enumerate(cookies):
        if not isinstance(c, dict):
            raise StorageStateValidationError(f"cookie {i} is not an object")
        if not isinstance(c.get("name"), str) or "value" not in c:
            raise StorageStateValidationError(f"cookie {i} missing name/value")
        if not (isinstance(c.get("domain"), str) or isinstance(c.get("url"), str)):
            raise StorageStateValidationError(f"cookie {i} missing domain/url")

    for i, o in enumerate(origins):
        if not isinstance(o, dict) or not isinstance(o.get("origin"), str):
            raise StorageStateValidationError(f"origin {i} missing 'origin'")

    return state


def write_storage_state(state: dict[str, Any], slug: str = DELTA_SLUG) -> datetime:
    """Write a validated storage_state into the slug's persistent-context dir,
    overwriting any existing one. Atomic (temp file + os.replace) so a
    concurrent launch never reads a half-written file; 0600 perms (best-effort).
    Returns the write timestamp (UTC). Never logs the contents."""
    target = storage_state_path(slug)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(state).encode("utf-8")

    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    finally:
        with contextlib.suppress(OSError):
            if os.path.exists(tmp):
                os.remove(tmp)

    logger.info(
        "delta_session.uploaded",
        slug=slug,
        cookies=len(state.get("cookies") or []),
        bytes=len(data),
    )
    return datetime.now(UTC)


def session_status(slug: str = DELTA_SLUG) -> dict[str, Any]:
    """Report whether an uploaded session is present (cheap, no browser launch).

    NEVER returns cookie names or values — only a count and a coarse heuristic
    (`has_delta_cookies`) plus the file's last-modified time.
    """
    path = storage_state_path(slug)
    if not path.exists():
        return {
            "slug": slug,
            "present": False,
            "updated_at": None,
            "cookie_count": 0,
            "has_delta_cookies": False,
        }

    updated_at: str | None = None
    with contextlib.suppress(OSError):
        updated_at = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()

    cookie_count = 0
    has_delta_cookies = False
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        cookies = state.get("cookies") if isinstance(state, dict) else None
        if isinstance(cookies, list):
            cookie_count = len(cookies)
            has_delta_cookies = any(
                isinstance(c, dict) and "delta" in str(c.get("domain", "")).lower()
                for c in cookies
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("delta_session.status_parse_failed", slug=slug, error=str(exc))

    return {
        "slug": slug,
        "present": True,
        "updated_at": updated_at,
        "cookie_count": cookie_count,
        "has_delta_cookies": has_delta_cookies,
    }


async def test_session(
    *,
    bridge: Any | None = None,
    adapter: Any | None = None,
    slug: str = DELTA_SLUG,
) -> dict[str, Any]:
    """Confirm the uploaded session actually works, before a real fetch.

    Opens the slug's context headless and runs the EXISTING `is_authenticated`
    probe (navigate to Response Manager, wait for the logged-in marker), then
    reports a boolean. It does NOT click, register interest, or otherwise change
    state — `is_authenticated` only navigates and reads the DOM. The context is
    closed afterwards so this probe doesn't hold Delta's single concurrent
    session.

    `bridge`/`adapter` are injectable for tests; in production they default to
    the in-process bridge client and the real Delta adapter.
    """
    from tender_agent.services.bridge_client import make_bridge_client

    bridge = bridge if bridge is not None else make_bridge_client()
    adapter = adapter if adapter is not None else DeltaEsourcingAdapter()

    if not await bridge.bridge_available():
        return {
            "available": False,
            "logged_in": False,
            "detail": "browser bridge / Playwright not available",
        }

    await bridge.open_session(slug, adapter.login_url())
    try:
        ctx = PortalContext(
            portal_id=0,
            user_id="operator",
            domain="",
            bridge=bridge,
            platform_slug=slug,
        )
        logged_in = await adapter.is_authenticated(ctx)
        current_url: str | None = None
        with contextlib.suppress(Exception):
            current_url = (await bridge.session_status(slug)).get("current_url")
        return {
            "available": True,
            "logged_in": bool(logged_in),
            "current_url": current_url,
        }
    finally:
        with contextlib.suppress(Exception):
            await bridge.close_session(slug)
