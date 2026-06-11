"""Reusable dashboard verification bot — Phase 0 of the build harness.

Drives the LIVE Genera Tenders dashboard (a real browser, real navigation,
real clicks) and reports whether parameterised check specs PASS / FAIL,
with the observed result attached. Designed to be the acceptance check for
every later phase: each phase ships a YAML/JSON of check specs, the bot
runs them, and a phase is DONE only when those checks come back green.

Layout:

* `specs.py`   — typed CheckSpec dataclasses (PURE, no Playwright). Each
                 spec encodes what to do (filter, navigate) + what to
                 assert (>0 rows, source present, CPV cell present, etc.).
* `report.py`  — readable text/JSON report (PURE).
* `parser.py`  — pure HTML extractors so unit tests can pin the bot's
                 reading of the dashboard's markup against saved fixtures.
* `bot.py`     — the Playwright driver (uses parser + specs). Lives here
                 so it can be imported with no Playwright runtime when only
                 specs/report/parser are needed (CI).
* `cli.py`     — `python -m tests.e2e.dashboard_bot` entry point.

The bot is split this way deliberately: from the project's egress
(Azure datacenter range) the live dashboard 403s — same disguise issue
the in-process bridge solves for Proactis. So CI runs the PURE bits
(unit tests); the operator runs the live driver from a residential IP or
via the same disguised in-process bridge. See README.md in this package.
"""
from __future__ import annotations

from tests.e2e.dashboard_bot.parser import (
    SearchResult,
    extract_results_from_dom,
    extract_source_chip_options,
)
from tests.e2e.dashboard_bot.report import BotReport, CheckResult, render_text
from tests.e2e.dashboard_bot.specs import (
    CheckOutcome,
    CheckSpec,
    FilterDraft,
    OpenTender,
    SearchExpectation,
    load_specs_from_yaml,
)

__all__ = [
    "BotReport",
    "CheckOutcome",
    "CheckResult",
    "CheckSpec",
    "FilterDraft",
    "OpenTender",
    "SearchExpectation",
    "SearchResult",
    "extract_results_from_dom",
    "extract_source_chip_options",
    "load_specs_from_yaml",
    "render_text",
]
