"""`python -m tests.e2e.dashboard_bot` — run a YAML of check specs.

Usage:

    python -m tests.e2e.dashboard_bot tests/e2e/specs/phase-0.yaml
    python -m tests.e2e.dashboard_bot --json tests/e2e/specs/phase-0.yaml

Operator workflow:
1. Each phase ships a `tests/e2e/specs/phase-N.yaml` with its acceptance
   checks. Phase 0's lives at `tests/e2e/specs/phase-0-self.yaml` — its
   ONLY job is to assert the bot can correctly read the dashboard's
   KNOWN current state from the handover.
2. The operator runs the bot from a residential IP (the deployed
   dashboard 403s the Azure datacenter range). Headed mode is supported
   with `--headed` for a watchable session.
3. Exit code is 0 only when every spec's observed outcome matched its
   expected outcome (PASS / OBSERVED_GAP both count as a match).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from tests.e2e.dashboard_bot.bot import DEFAULT_DASHBOARD_URL, DashboardBot
from tests.e2e.dashboard_bot.report import render_json, render_text
from tests.e2e.dashboard_bot.specs import load_specs_from_yaml


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dashboard_bot")
    parser.add_argument(
        "specs", type=Path, help="YAML file containing the check specs"
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("DASHBOARD_URL", DEFAULT_DASHBOARD_URL),
        help="Dashboard base URL (default: the deployed cloud URL)",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit a machine-readable JSON report"
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run with a visible browser (default: headless)",
    )
    parser.add_argument(
        "--screenshots",
        type=Path,
        default=None,
        help="If set, write screenshots of any FAIL into this directory",
    )
    args = parser.parse_args(argv)

    specs = load_specs_from_yaml(args.specs)

    async def _run() -> int:
        async with DashboardBot(
            dashboard_url=args.url,
            headless=not args.headed,
            screenshot_dir=str(args.screenshots) if args.screenshots else None,
        ) as bot:
            report = await bot.run_specs(specs)
        text = render_json(report) if args.json else render_text(report)
        print(text)
        return 0 if report.passed else 2

    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
