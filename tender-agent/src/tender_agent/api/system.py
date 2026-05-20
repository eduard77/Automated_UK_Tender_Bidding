"""System status endpoints (bridge health, etc.)."""
from __future__ import annotations

from fastapi import APIRouter

from tender_agent.services.bridge_client import BridgeClient

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/bridge-health")
async def bridge_health() -> dict:
    """Proxy the native browser bridge's health so the dashboard can show a
    one-glance up/down indicator without reaching the host directly."""
    available = await BridgeClient().bridge_available()
    return {"available": available}
