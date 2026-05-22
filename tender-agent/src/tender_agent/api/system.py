"""System status endpoints (bridge health, preflight diagnostics, etc.)."""
from __future__ import annotations

from fastapi import APIRouter

from tender_agent.services.bridge_client import BridgeClient
from tender_agent.services.preflight import run_preflight

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/bridge-health")
async def bridge_health() -> dict:
    """Proxy the native browser bridge's health so the dashboard can show a
    one-glance up/down indicator without reaching the host directly. Uses the
    configured bridge URL + token (BridgeClient)."""
    available = await BridgeClient().bridge_available()
    return {"available": available}


@router.get("/preflight")
async def preflight() -> dict:
    """Self-diagnosing wiring check: is the documents dir writable, is the bridge
    token set, and is the bridge reachable? Returns a structured result so a
    first-run misconfiguration explains itself instead of failing cryptically."""
    result = await run_preflight(check_bridge=True)
    return result.as_dict()
