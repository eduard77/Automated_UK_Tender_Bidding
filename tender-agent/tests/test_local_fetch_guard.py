"""The cloud's local-fetch guard (`_local_fetch_block`) — pure unit, no DB.

Confirms the cloud refuses configured platforms (Delta) unless it IS the local
fetch runner, and never blocks other platforms.
"""
from __future__ import annotations

from tender_agent.config import settings
from tender_agent.services.portal_orchestrator import PortalOrchestrator


def test_blocks_delta_in_cloud(monkeypatch):
    monkeypatch.setattr(settings, "local_fetch_runner", False)
    monkeypatch.setattr(settings, "local_fetch_platforms", ["delta_esourcing"])
    reason = PortalOrchestrator()._local_fetch_block("delta_esourcing")
    assert reason is not None and "fetch_delta.py" in reason


def test_local_runner_bypasses_block(monkeypatch):
    monkeypatch.setattr(settings, "local_fetch_runner", True)
    monkeypatch.setattr(settings, "local_fetch_platforms", ["delta_esourcing"])
    assert PortalOrchestrator()._local_fetch_block("delta_esourcing") is None


def test_other_platforms_not_blocked(monkeypatch):
    monkeypatch.setattr(settings, "local_fetch_runner", False)
    monkeypatch.setattr(settings, "local_fetch_platforms", ["delta_esourcing"])
    assert PortalOrchestrator()._local_fetch_block("contracts_finder_direct") is None
    assert PortalOrchestrator()._local_fetch_block(None) is None
