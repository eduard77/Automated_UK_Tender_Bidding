"""LIVE dashboard-bot smoke test — marked `e2e`, skipped by default.

Runs the bot against the deployed dashboard URL using the Phase 0
self-test YAML and asserts every spec's observed verdict matches its
declared expected verdict. This is the gate Phase 0 needs to clear
end-to-end.

Skipped unless `E2E=1` (or `DASHBOARD_BOT_LIVE=1`) is set in the
environment, because the deployed dashboard 403s the project's CI
runner range. The operator runs this from a residential IP:

    cd tender-agent
    E2E=1 pytest -v tests/e2e/test_dashboard_bot_live.py

The same YAML is runnable as a standalone CLI:

    python -m tests.e2e.dashboard_bot tests/e2e/specs/phase-0-self.yaml
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.e2e.dashboard_bot.bot import DEFAULT_DASHBOARD_URL, DashboardBot
from tests.e2e.dashboard_bot.specs import load_specs_from_yaml

_SKIP_REASON = (
    "Live dashboard test — set E2E=1 to enable. Skipped by default because "
    "the dashboard's deployed URL 403s the project's CI datacenter range."
)


pytestmark = pytest.mark.skipif(
    not (os.environ.get("E2E") or os.environ.get("DASHBOARD_BOT_LIVE")),
    reason=_SKIP_REASON,
)


@pytest.mark.e2e
async def test_phase_0_self_specs_against_the_live_dashboard() -> None:
    spec_path = Path(__file__).parent / "specs" / "phase-0-self.yaml"
    specs = load_specs_from_yaml(spec_path)
    url = os.environ.get("DASHBOARD_URL", DEFAULT_DASHBOARD_URL)
    async with DashboardBot(dashboard_url=url, headless=True) as bot:
        report = await bot.run_specs(specs)

    failed = [r for r in report.results if not r.passed]
    if failed:
        from tests.e2e.dashboard_bot.report import render_text

        pytest.fail("dashboard bot reported failures:\n" + render_text(report))
