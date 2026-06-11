"""Bot run report — pure rendering.

A run produces one `CheckResult` per spec; the suite renders a readable
text report (the CLI prints it) and a JSON one (CI / log capture). The
report is intentionally narrow: enough to triage every verdict in one
glance, without leaking the dashboard URL, cookies, or anything looking
like a credential.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from tests.e2e.dashboard_bot.specs import CheckOutcome, CheckSpec


@dataclass
class CheckResult:
    """One spec's outcome — the bot's observation + the matched verdict.

    `details` is a list of short bullet lines: e.g. ['source_chips: CF,
    FTS, PCS, PROACTIS, SAMPLE_SEED', 'expected min_results>=1, got 25'].
    `screenshot_path` is set by the live driver when it captured one;
    None on pure-logic paths (and on PASS where no screenshot needed).
    """

    spec_name: str
    rationale: str
    expected: CheckOutcome
    observed: CheckOutcome
    details: list[str] = field(default_factory=list)
    error: str | None = None
    screenshot_path: str | None = None

    @property
    def passed(self) -> bool:
        return self.observed == self.expected


@dataclass
class BotReport:
    """A whole run's worth of `CheckResult`s, ordered as the specs ran.

    `dashboard_url` is recorded for the operator's audit trail (no
    cookies, no auth headers). `started_at` and `finished_at` are ISO
    UTC; the suite is happy with both `None` for a unit-test build that
    didn't actually drive a browser."""

    dashboard_url: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    results: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def pass_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.results if not r.passed)


def make_check_result(
    spec: CheckSpec,
    observed: CheckOutcome,
    details: list[str] | None = None,
    error: str | None = None,
    screenshot_path: str | None = None,
) -> CheckResult:
    return CheckResult(
        spec_name=spec.name,
        rationale=spec.rationale,
        expected=spec.expected_outcome,
        observed=observed,
        details=list(details or []),
        error=error,
        screenshot_path=screenshot_path,
    )


def render_text(report: BotReport) -> str:
    """A single-screen text report. One block per check.

    Format chosen so a curl-piping pipeline reads cleanly:

        [PASS] Phase-0/sources-present
            why  : the Source facet currently lists the five known sources.
            obs  : source_chips: CF, FTS, PCS, PROACTIS, SAMPLE_SEED
            (expected PASS — got PASS)
    """
    lines: list[str] = []
    if report.dashboard_url:
        lines.append(f"dashboard: {report.dashboard_url}")
    if report.started_at:
        lines.append(f"started: {_fmt(report.started_at)}")
    if report.finished_at:
        lines.append(f"finished: {_fmt(report.finished_at)}")
    lines.append("")
    for result in report.results:
        tag = "PASS" if result.passed else "FAIL"
        lines.append(f"[{tag}] {result.spec_name}")
        if result.rationale:
            lines.append(f"    why  : {result.rationale}")
        for detail in result.details:
            lines.append(f"    obs  : {detail}")
        if result.error:
            lines.append(f"    err  : {result.error}")
        if result.screenshot_path:
            lines.append(f"    shot : {result.screenshot_path}")
        lines.append(
            f"    (expected {result.expected.value} — got {result.observed.value})"
        )
        lines.append("")
    lines.append(
        f"summary: {report.pass_count} passed, {report.fail_count} failed"
    )
    return "\n".join(lines)


def render_json(report: BotReport) -> str:
    payload = asdict(report)
    payload["passed"] = report.passed
    payload["pass_count"] = report.pass_count
    payload["fail_count"] = report.fail_count
    # asdict serialises datetimes as datetimes — coerce to ISO so json
    # doesn't choke.
    for key in ("started_at", "finished_at"):
        if isinstance(payload.get(key), datetime):
            payload[key] = _fmt(payload[key])
    # Enum -> value
    for r in payload["results"]:
        r["expected"] = (
            r["expected"].value
            if isinstance(r["expected"], CheckOutcome)
            else r["expected"]
        )
        r["observed"] = (
            r["observed"].value
            if isinstance(r["observed"], CheckOutcome)
            else r["observed"]
        )
    return json.dumps(payload, indent=2, default=str)


def _fmt(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()
