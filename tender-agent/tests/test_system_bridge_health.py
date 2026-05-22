"""GET /system/bridge-health proxies the bridge availability probe."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tender_agent.main import app


@pytest.fixture()
def client():
    return TestClient(app)


def test_bridge_health_up(monkeypatch, client):
    async def _up(self):
        return True

    monkeypatch.setattr(
        "tender_agent.api.system.BridgeClient.bridge_available", _up
    )
    resp = client.get("/system/bridge-health")
    assert resp.status_code == 200
    assert resp.json() == {"available": True}


def test_bridge_health_down(monkeypatch, client):
    async def _down(self):
        return False

    monkeypatch.setattr(
        "tender_agent.api.system.BridgeClient.bridge_available", _down
    )
    resp = client.get("/system/bridge-health")
    assert resp.status_code == 200
    assert resp.json() == {"available": False}
