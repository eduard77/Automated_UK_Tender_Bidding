"""`python -m bridge` — start the browser bridge on 0.0.0.0:8765."""
from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("TENDER_AGENT_BRIDGE_HOST", "0.0.0.0")
    port = int(os.environ.get("TENDER_AGENT_BRIDGE_PORT", "8765"))
    if not os.environ.get("TENDER_AGENT_BRIDGE_TOKEN"):
        print(
            "WARNING: TENDER_AGENT_BRIDGE_TOKEN is not set. Every request will be "
            "rejected with 401 until you set it (same value as the backend's .env)."
        )
    print(f"Browser bridge starting on http://localhost:{port} — leave this window open.")
    uvicorn.run("bridge.server:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
