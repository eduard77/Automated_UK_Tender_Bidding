"""Self-diagnosing preflight checks for the backend↔bridge wiring.

These turn cryptic first-run failures — a PermissionError on a bare relative
'data' path, a missing/empty bridge token, a bridge that isn't running — into
clear, actionable messages. The same checks back two callers:

* ``GET /system/preflight`` (and an app-startup log) so an operator can see at a
  glance what's wrong, and
* the orchestrator, which runs them BEFORE a login-portal fetch so the task
  fails with a useful detail instead of a raw exception after the human has
  already logged in.

The bridge-reachability probe is best-effort and never fatal: the bridge may
legitimately be off (the user starts it on demand).
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from tender_agent.config import settings
from tender_agent.services.bridge_client import make_bridge_client

logger = structlog.get_logger(__name__)


@dataclass
class PreflightResult:
    documents_dir_writable: bool
    bridge_token_set: bool
    bridge_reachable: bool
    details: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "documents_dir_writable": self.documents_dir_writable,
            "bridge_token_set": self.bridge_token_set,
            "bridge_reachable": self.bridge_reachable,
            "details": list(self.details),
        }


def check_documents_dir_writable() -> tuple[bool, str]:
    """Whether DOCUMENT_STORAGE_DIR can be created and written. We actually
    create the directory and a temp file (then remove it) — a stat() isn't
    enough to prove the app user can write inside a mounted volume."""
    path = settings.document_storage_dir
    probe = Path(path) / ".preflight-write-test"
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"ok")
    except Exception as exc:  # noqa: BLE001
        return False, (
            f"Documents directory {path} is not writable "
            f"({type(exc).__name__}: {exc}) — check the volume mount in "
            f"docker-compose.yml and DOCUMENT_STORAGE_DIR."
        )
    finally:
        with contextlib.suppress(OSError):
            probe.unlink()
    return True, f"Documents directory {path} exists and is writable."


def check_bridge_token() -> tuple[bool, str]:
    """Whether the shared bridge token is configured (non-empty)."""
    if (settings.bridge_token or "").strip():
        return True, "Bridge token is configured."
    return False, (
        "Bridge token not configured — set TENDER_AGENT_BRIDGE_TOKEN in "
        "tender-agent/.env to match browser-bridge/.env."
    )


async def check_bridge_reachable() -> tuple[bool, str]:
    """Best-effort, short-timeout probe of the bridge /health endpoint. Down is
    reported but never treated as fatal — the bridge runs on demand."""
    try:
        available = await make_bridge_client().bridge_available()
    except Exception as exc:  # noqa: BLE001
        return False, f"Bridge health probe failed ({type(exc).__name__}: {exc})."
    if available:
        return True, f"Bridge reachable at {settings.bridge_url}."
    return False, (
        f"Bridge not reachable at {settings.bridge_url} — this is fine if the "
        f"bridge isn't running; start start-bridge.ps1 on your PC to fetch from "
        f"login portals."
    )


async def run_preflight(check_bridge: bool = True) -> PreflightResult:
    """Run all checks and return a structured result. Never raises — each check
    degrades to a False + detail string. Logs the outcome so a first-run failure
    explains itself in the app logs as well as at /system/preflight."""
    docs_ok, docs_detail = check_documents_dir_writable()
    token_ok, token_detail = check_bridge_token()
    details = [docs_detail, token_detail]

    bridge_ok = False
    if check_bridge:
        bridge_ok, bridge_detail = await check_bridge_reachable()
        details.append(bridge_detail)

    result = PreflightResult(
        documents_dir_writable=docs_ok,
        bridge_token_set=token_ok,
        bridge_reachable=bridge_ok,
        details=details,
    )

    logger.info("preflight.checked", **result.as_dict())
    if not docs_ok:
        logger.warning("preflight.documents_dir_not_writable", detail=docs_detail)
    if not token_ok:
        logger.warning("preflight.bridge_token_missing", detail=token_detail)
    if check_bridge and not bridge_ok:
        logger.info("preflight.bridge_unreachable", detail=details[-1])

    return result
