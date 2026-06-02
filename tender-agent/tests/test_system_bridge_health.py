"""GET /system/bridge-health proxies the bridge availability probe."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tender_agent.main import app


@pytest.fixture()
def client():
    return TestClient(app)


def _patch_bridge(monkeypatch, module_path: str, available: bool) -> None:
    """Stand-in for the bridge: monkeypatch the module's
    `make_bridge_client` so it returns a fake whose `bridge_available()`
    yields the requested bool. Works for both HTTP and in-process
    implementations — the endpoint only cares about that bool."""

    class _Fake:
        async def bridge_available(self):
            return available

    monkeypatch.setattr(module_path, lambda: _Fake())


def test_bridge_health_up(monkeypatch, client):
    _patch_bridge(monkeypatch, "tender_agent.api.system.make_bridge_client", True)
    resp = client.get("/system/bridge-health")
    assert resp.status_code == 200
    assert resp.json() == {"available": True}


def test_bridge_health_down(monkeypatch, client):
    _patch_bridge(monkeypatch, "tender_agent.api.system.make_bridge_client", False)
    resp = client.get("/system/bridge-health")
    assert resp.status_code == 200
    assert resp.json() == {"available": False}


def test_preflight_endpoint_returns_structured_result(monkeypatch, client, tmp_path):
    monkeypatch.setattr(
        "tender_agent.services.preflight.settings.document_storage_dir",
        str(tmp_path / "docs"),
    )
    monkeypatch.setattr(
        "tender_agent.services.preflight.settings.bridge_token", "TA-secret"
    )
    _patch_bridge(
        monkeypatch, "tender_agent.services.preflight.make_bridge_client", True
    )
    resp = client.get("/system/preflight")
    assert resp.status_code == 200
    body = resp.json()
    assert body["documents_dir_writable"] is True
    assert body["bridge_token_set"] is True
    assert body["bridge_reachable"] is True
    assert isinstance(body["details"], list)
