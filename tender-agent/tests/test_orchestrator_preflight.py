"""Orchestrator preflight guard: before a login-portal fetch, a non-writable
documents dir or a missing bridge token fails the task with a CLEAR, actionable
message — not a raw PermissionError / silent 401 (issue 4d).

These run without a DB or network: the guard returns before any DB/bridge use.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tender_agent.services import preflight as pf
from tender_agent.services.portal_orchestrator import (
    OrchestrationStatus,
    PortalOrchestrator,
)


def _tender():
    return SimpleNamespace(id=1)


async def _call(orch):
    return await orch._run_login_adapter(
        None,  # db — never touched; the guard returns first
        object(),  # adapter — not used before the guard returns
        _tender(),
        None,  # portal
        "delta_esourcing",
        "DeltaEsourcingAdapter",
        "eduard",
        [],  # candidate_urls
        None,  # task_id
        False,  # resume_from_confirm
    )


@pytest.mark.asyncio
async def test_non_writable_documents_dir_fails_with_clear_message(tmp_path, monkeypatch):
    # Storage dir routed through a file -> not creatable/writable.
    a_file = tmp_path / "not-a-dir"
    a_file.write_bytes(b"x")
    monkeypatch.setattr(pf.settings, "document_storage_dir", str(a_file / "sub"))
    monkeypatch.setattr(pf.settings, "bridge_token", "TA-secret")

    res = await _call(PortalOrchestrator())

    assert res.status == OrchestrationStatus.error
    assert "not writable" in res.detail
    assert "DOCUMENT_STORAGE_DIR" in res.detail


@pytest.mark.asyncio
async def test_missing_bridge_token_fails_with_clear_message(tmp_path, monkeypatch):
    monkeypatch.setattr(pf.settings, "document_storage_dir", str(tmp_path / "docs"))
    monkeypatch.setattr(pf.settings, "bridge_token", "")

    res = await _call(PortalOrchestrator())

    assert res.status == OrchestrationStatus.error
    assert "TENDER_AGENT_BRIDGE_TOKEN" in res.detail
    # Actionable: tells the user where to set it.
    assert "browser-bridge/.env" in res.detail


@pytest.mark.asyncio
async def test_injected_bridge_skips_guard(tmp_path, monkeypatch):
    """A caller that injects a bridge owns its environment, so the guard is
    skipped — proving the guard is scoped to real first-runs. With an injected
    down bridge we fall through to the normal bridge_unavailable result."""
    # Even with a broken storage dir + empty token, the guard must not fire.
    a_file = tmp_path / "not-a-dir"
    a_file.write_bytes(b"x")
    monkeypatch.setattr(pf.settings, "document_storage_dir", str(a_file / "sub"))
    monkeypatch.setattr(pf.settings, "bridge_token", "")

    class _DownBridge:
        async def bridge_available(self):
            return False

    orch = PortalOrchestrator(bridge=_DownBridge())
    res = await _call(orch)

    assert res.status == OrchestrationStatus.bridge_unavailable
