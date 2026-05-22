"""Bridge configuration from environment.

The bridge runs natively on the user's Windows desktop (outside Docker). It
shares TENDER_AGENT_BRIDGE_TOKEN with the backend so only the backend can drive
the user's logged-in browser.
"""
from __future__ import annotations

import os
from pathlib import Path


def _home() -> Path:
    # %USERPROFILE% on Windows, $HOME elsewhere.
    return Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))


def bridge_token() -> str:
    return os.environ.get("TENDER_AGENT_BRIDGE_TOKEN", "")


def state_dir() -> Path:
    env = os.environ.get("TENDER_AGENT_BRIDGE_STATE_DIR")
    base = Path(env) if env else _home() / ".tender-agent" / "bridge-sessions"
    base.mkdir(parents=True, exist_ok=True)
    return base


def download_dir() -> Path:
    env = os.environ.get("TENDER_AGENT_BRIDGE_DOWNLOAD_DIR")
    base = Path(env) if env else _home() / ".tender-agent" / "bridge-downloads"
    base.mkdir(parents=True, exist_ok=True)
    return base


def headless() -> bool:
    return os.environ.get("TENDER_AGENT_BRIDGE_HEADLESS", "").strip() in ("1", "true", "True")


def session_dir(slug: str) -> Path:
    # Defensive: slugs are short identifiers; never let one escape state_dir.
    safe = "".join(c for c in slug if c.isalnum() or c in ("-", "_")) or "default"
    d = state_dir() / safe
    d.mkdir(parents=True, exist_ok=True)
    return d
