"""Dashboard-bot unit tests — pure, no Playwright, run in CI.

Three things this file pins:

1. **Parser** reads the dashboard's real component output correctly
   (against the committed fixtures in tests/e2e/fixtures/, which mirror
   the live SearchPage / ResultCard markup as of 2026-06-11).
2. **YAML loader** lifts the operator's spec files into typed CheckSpecs
   without losing fields, and accepts both snake_case and camelCase keys.
3. **Verdict logic** (`bot._verdict_for_search`) returns the right
   CheckOutcome for the assertions Phase 0 actually needs, including the
   OBSERVED_GAP path that the handover's "Proactis carries no CPV today"
   ends up on.

The LIVE driver is exercised separately in test_dashboard_bot_live.py
and is marked + skipped by default (CI is in the Azure-datacenter
range — the live URL 403s us; see README.md for the runbook).
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from tests.e2e.dashboard_bot.bot import (
    _friendly_source_label,
    _verdict_for_search,
)
from tests.e2e.dashboard_bot.parser import (
    SearchResult,
    extract_cpv_cell,
    extract_results_from_dom,
    extract_source_chip_options,
)
from tests.e2e.dashboard_bot.report import (
    BotReport,
    make_check_result,
    render_json,
    render_text,
)
from tests.e2e.dashboard_bot.specs import (
    CheckOutcome,
    CheckSpec,
    SearchExpectation,
    load_specs_from_yaml,
)

_E2E_FIX = Path(__file__).parent / "fixtures"


def _e2e_fixture(name: str) -> str:
    """Read a fixture from `tests/e2e/fixtures/`. The standard
    `load_text_fixture` looks under `tests/fixtures/`."""
    return (_E2E_FIX / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Parser — selectors vs the dashboard's real component output
# ---------------------------------------------------------------------------


def test_extract_source_chip_options_lists_the_handover_five_sources() -> None:
    html = _e2e_fixture("dashboard_search_5_sources.html")
    chips = extract_source_chip_options(html)
    assert chips == [
        "Contracts Finder",
        "Find a Tender",
        "Public Contracts Scotland",
        "Proactis / ProContract",
        "SAMPLE_SEED",
    ]


def test_extract_source_chip_options_handles_empty_panel() -> None:
    assert extract_source_chip_options("") == []
    assert extract_source_chip_options("<div>no chips here</div>") == []


def test_extract_results_returns_one_per_card_with_source_and_title() -> None:
    html = _e2e_fixture("dashboard_search_5_sources.html")
    results = extract_results_from_dom(html)
    assert len(results) == 5
    by_source = {r.source_code: r for r in results}
    assert set(by_source) == {"CF", "FTS", "PCS", "PROACTIS", "SAMPLE_SEED"}
    assert by_source["PROACTIS"].tender_id == 104
    assert "Greater Manchester" in by_source["PROACTIS"].title
    assert by_source["CF"].tender_id == 101


def test_extract_results_returns_empty_for_no_match_dom() -> None:
    html = _e2e_fixture("dashboard_search_empty.html")
    assert extract_results_from_dom(html) == []


def test_extract_cpv_cell_reads_cf_detail() -> None:
    html = _e2e_fixture("dashboard_tender_detail_cf_with_cpv.html")
    assert extract_cpv_cell(html) == "45000000, 45111100"


def test_extract_cpv_cell_returns_none_when_proactis_detail_omits_it() -> None:
    """Handover §3: Proactis tenders carry no CPV today. The detail page
    still renders the CPV row label but its value cell is empty."""
    html = _e2e_fixture("dashboard_tender_detail_proactis_no_cpv.html")
    assert extract_cpv_cell(html) is None


# ---------------------------------------------------------------------------
# YAML loader
# ---------------------------------------------------------------------------


def test_load_specs_from_yaml_parses_phase_0_self_file() -> None:
    path = Path(__file__).parent / "specs" / "phase-0-self.yaml"
    specs = load_specs_from_yaml(path)
    assert len(specs) == 5
    names = [s.name for s in specs]
    assert "Phase-0/proactis-rows-exist" in names
    assert "Phase-0/cpv-filter-returns-nothing-for-proactis-today" in names
    # The OBSERVED_GAP expectation is in there for the normalisation gap.
    gap_spec = next(
        s for s in specs if s.name.endswith("returns-nothing-for-proactis-today")
    )
    assert gap_spec.expected_outcome == CheckOutcome.OBSERVED_GAP
    assert gap_spec.filter is not None
    assert gap_spec.filter.cpv == "45000000"
    assert gap_spec.filter.sources == ["PROACTIS"]
    assert gap_spec.search is not None
    assert gap_spec.search.max_results == 0


def test_load_specs_accepts_camelcase_keys(tmp_path: Path) -> None:
    src = textwrap.dedent(
        """
        - name: camel-case-spec
          rationale: lift camelCase into the typed dataclasses
          filter:
            sources: [PROACTIS]
            valueMin: "10000"
          search:
            minResults: 1
            mustIncludeSources: [PROACTIS]
            mustExcludeSources: []
          open:
            resultIndex: 0
            cpvFieldPresent: true
        """
    )
    p = tmp_path / "camel.yaml"
    p.write_text(src, encoding="utf-8")
    specs = load_specs_from_yaml(p)
    spec = specs[0]
    assert spec.filter is not None and spec.filter.value_min == "10000"
    assert spec.search is not None and spec.search.min_results == 1
    assert spec.search.must_include_sources == ["PROACTIS"]
    assert spec.open is not None and spec.open.cpv_field_present is True


# ---------------------------------------------------------------------------
# Verdict logic
# ---------------------------------------------------------------------------


def _r(source: str, tid: int = 1) -> SearchResult:
    return SearchResult(tender_id=tid, title="t", source_code=source)


def test_verdict_passes_min_max_and_inclusion_checks() -> None:
    details: list[str] = []
    verdict = _verdict_for_search(
        SearchExpectation(
            min_results=1,
            max_results=10,
            must_include_sources=["PROACTIS"],
            must_exclude_sources=["ATAMIS"],
        ),
        [_r("PROACTIS"), _r("CF", 2)],
        details,
    )
    assert verdict == CheckOutcome.PASS


def test_verdict_fails_when_min_results_unsatisfied() -> None:
    details: list[str] = []
    verdict = _verdict_for_search(
        SearchExpectation(min_results=1), [], details
    )
    assert verdict == CheckOutcome.FAIL
    assert any("min_results" in d for d in details)


def test_verdict_fails_when_required_source_absent() -> None:
    details: list[str] = []
    verdict = _verdict_for_search(
        SearchExpectation(must_include_sources=["PROACTIS"]),
        [_r("CF")],
        details,
    )
    assert verdict == CheckOutcome.FAIL


def test_verdict_fails_when_forbidden_source_present() -> None:
    details: list[str] = []
    verdict = _verdict_for_search(
        SearchExpectation(must_exclude_sources=["ATAMIS"]),
        [_r("ATAMIS")],
        details,
    )
    assert verdict == CheckOutcome.FAIL


def test_max_results_zero_is_satisfied_by_empty_results() -> None:
    """The OBSERVED_GAP spec for "CPV filter returns nothing for Proactis"
    asserts max_results=0 with an empty result list — must be PASS."""
    details: list[str] = []
    verdict = _verdict_for_search(
        SearchExpectation(max_results=0), [], details
    )
    assert verdict == CheckOutcome.PASS


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _spec(name: str, expected: CheckOutcome = CheckOutcome.PASS) -> CheckSpec:
    return CheckSpec(name=name, rationale="why", expected_outcome=expected)


def test_render_text_marks_pass_when_observed_matches_expected() -> None:
    report = BotReport(
        results=[
            make_check_result(
                _spec("a"), observed=CheckOutcome.PASS, details=["chips: CF, FTS"]
            ),
            make_check_result(
                _spec("b", CheckOutcome.OBSERVED_GAP),
                observed=CheckOutcome.OBSERVED_GAP,
            ),
        ]
    )
    text = render_text(report)
    assert "[PASS] a" in text
    assert "[PASS] b" in text  # OBSERVED_GAP that was expected is still a pass
    assert "chips: CF, FTS" in text
    assert "summary: 2 passed, 0 failed" in text
    assert report.passed is True


def test_render_text_marks_fail_when_observation_diverges() -> None:
    report = BotReport(
        results=[
            make_check_result(
                _spec("a", CheckOutcome.PASS),
                observed=CheckOutcome.FAIL,
                details=["min_results: expected >= 1, got 0"],
                error="no rows",
            ),
        ]
    )
    text = render_text(report)
    assert "[FAIL] a" in text
    assert "err  : no rows" in text
    assert "summary: 0 passed, 1 failed" in text
    assert report.passed is False


def test_render_json_round_trips_outcomes_and_summary() -> None:
    report = BotReport(
        dashboard_url="https://example.test",
        results=[
            make_check_result(
                _spec("a", CheckOutcome.OBSERVED_GAP),
                observed=CheckOutcome.OBSERVED_GAP,
            ),
        ],
    )
    blob = render_json(report)
    assert '"expected": "OBSERVED_GAP"' in blob
    assert '"observed": "OBSERVED_GAP"' in blob
    assert '"passed": true' in blob


# ---------------------------------------------------------------------------
# Friendly-label map mirrors the dashboard's lib/format.ts SOURCE_LABELS
# ---------------------------------------------------------------------------


def test_friendly_label_covers_every_source_in_the_handover() -> None:
    # Handover §2: five sources currently render in the dashboard. Plus
    # the three Phase 1 / future arrivals — Atamis, EU-Supply, Sell2Wales —
    # whose chip labels we already need for spec-side filtering.
    for code, expected in {
        "CF": "Contracts Finder",
        "FTS": "Find a Tender",
        "PCS": "Public Contracts Scotland",
        "PROACTIS": "Proactis / ProContract",
        "SAMPLE_SEED": "SAMPLE_SEED",
        "ATAMIS": "Atamis",
        "EU_SUPPLY": "EU-Supply / Mercell",
        "S2W": "Sell2Wales",
        "NI": "eTendersNI",
    }.items():
        assert _friendly_source_label(code) == expected, code


def test_friendly_label_passes_unknown_codes_through() -> None:
    assert _friendly_source_label("UNKNOWN_FUTURE_SOURCE") == "UNKNOWN_FUTURE_SOURCE"


# ---------------------------------------------------------------------------
# A consumer of the public package surface — keeps the __init__ honest
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
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
    ],
)
def test_public_surface_exports_the_named_symbol(name: str) -> None:
    import tests.e2e.dashboard_bot as pkg

    assert hasattr(pkg, name), f"dashboard_bot.{name} is not exported"
