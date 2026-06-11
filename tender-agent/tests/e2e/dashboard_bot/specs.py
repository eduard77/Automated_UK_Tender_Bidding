"""Typed check specs — pure data, no Playwright.

A `CheckSpec` is what a phase asks the bot to verify on the dashboard:
"set these filters, then assert this expectation". Specs are loadable from
YAML so later phases append checks without touching code, and the bot's
unit tests can exercise the parser/validator without a browser.

Three building blocks:

* `FilterDraft`  — the filter state to set BEFORE pressing Search. Mirrors
  the SearchPage form: free-text Search, CPV list, buyer string, value
  range, source chip list, etc. None / empty means "leave that control
  alone" — Search isn't pressed implicitly; the spec drives it.
* `SearchExpectation` — what to assert about the post-Search results
  list: `min_results`, source-rollup counts, "every visible result must
  have a CPV cell", "no result has source X". Each assertion is opt-in
  and ANDed.
* `OpenTender` — after the result list renders, optionally click into the
  Nth result and assert facts about its detail page (e.g. CPV row
  present). Lets a single spec say "Proactis tenders exist AND none of
  them carry CPV today" — which is the very gap the handover names.

Each CheckSpec carries an explicit `expected_outcome`: typically `PASS`,
but for spec 4 of Phase 0 ("CPV filter currently returns nothing for
Proactis") we expect `OBSERVED_GAP` — the bot truthfully reports the gap
and that IS the desired pass for now. When the gap closes (Phase 3
normalisation), flipping the expected_outcome to `PASS` is the bot's
proof.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class CheckOutcome(StrEnum):
    """The three verdicts a spec can declare it wants the bot to observe.

    PASS         — the bot's observation matches the expectation.
    OBSERVED_GAP — the bot truthfully reports a KNOWN current gap (e.g.
                   "Proactis tenders exist but none carry a CPV today").
                   This counts as a pass in Phase 0; flipping the
                   expectation to PASS after a later phase closes the gap
                   is the proof Phase 3+ landed.
    FAIL         — observation does not match; the bot prints the
                   difference and the run returns non-zero.
    """

    PASS = "PASS"
    OBSERVED_GAP = "OBSERVED_GAP"
    FAIL = "FAIL"


@dataclass
class FilterDraft:
    """Filter values to type/click into the SearchPage form before Search.

    Field names match the form labels — `q` ('Search text'), `cpv` ('CPV
    codes'), `buyer`, `value_min`/`value_max` (`£` prefix stripped at
    submit time), `sources` (source codes to TOGGLE on — the friendly
    label is rendered via sourceLabel; we click by the code's chip text).
    `None`/`[]` leaves the control untouched, so a spec that only filters
    by Source doesn't accidentally clear CPV.
    """

    q: str | None = None
    cpv: str | None = None
    buyer: str | None = None
    value_min: str | None = None
    value_max: str | None = None
    sources: list[str] = field(default_factory=list)


@dataclass
class SearchExpectation:
    """What to assert about the results list after Search.

    Each field is opt-in; unset means "don't check". They are ANDed —
    every set assertion must hold. The bot records what it OBSERVED for
    each, so a fail shows expected-vs-observed side by side.

    `min_results`            — at least N result cards rendered.
    `max_results`            — at most N result cards rendered.
    `must_include_sources`   — every named source code must appear in at
                               least one rendered card (post-pagination
                               page-1 only — the bot doesn't paginate
                               unless `paginate: true` is set on the spec).
    `must_exclude_sources`   — no card may carry these source codes.
    `every_card_has_cpv`     — for every card, the visible CPV cell is
                               non-empty (used to assert the Phase-3 fix
                               populates CPV for Proactis).
    `cpv_present_for_source` — same but scoped to cards of a specific
                               source code. Maps "for any Proactis card,
                               its CPV cell is non-empty"; today it's the
                               OBSERVED_GAP path.
    """

    min_results: int | None = None
    max_results: int | None = None
    must_include_sources: list[str] = field(default_factory=list)
    must_exclude_sources: list[str] = field(default_factory=list)
    every_card_has_cpv: bool | None = None
    cpv_present_for_source: str | None = None


@dataclass
class OpenTender:
    """Click into the Nth result (0-indexed) and assert facts about the
    detail page. `cpv_field_present` is the relevant one for Phase 0 —
    the handover says Proactis details lack a CPV; flipping the
    expectation later proves Phase 3 is real."""

    result_index: int = 0
    cpv_field_present: bool | None = None
    title_non_empty: bool | None = None


@dataclass
class CheckSpec:
    """One bot check — what to do, what to expect, why.

    `name`               human label that appears in the report.
    `rationale`          one-line "why this check exists"; appears beside
                         the verdict so the report is self-explanatory.
    `expected_outcome`   PASS / OBSERVED_GAP / FAIL — the verdict the spec
                         author predicts. Mismatch IS the failure.
    `filter`             FilterDraft to set + Search. Optional — a spec
                         that only opens a known tender by id can skip.
    `search`             SearchExpectation; required when `filter` is set.
    `open`               OpenTender, run AFTER the search if present.
    `tags`               free strings — phase id, ticket, etc.
    """

    name: str
    rationale: str
    expected_outcome: CheckOutcome = CheckOutcome.PASS
    filter: FilterDraft | None = None
    search: SearchExpectation | None = None
    open: OpenTender | None = None
    tags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# YAML loader — accepts the camelCase / snake_case the operator finds
# convenient and normalises to the dataclasses above.
# ---------------------------------------------------------------------------


def _first(d: dict[str, Any], *keys: str) -> Any:
    """Return the FIRST present key's value (None when absent). Beats
    `a or b` for numeric/bool fields, where 0 / False would wrongly fall
    through — the very bug a max_results: 0 spec entry triggered."""
    for key in keys:
        if key in d:
            return d[key]
    return None


def _draft_from(d: dict[str, Any] | None) -> FilterDraft | None:
    if d is None:
        return None
    return FilterDraft(
        q=d.get("q"),
        cpv=d.get("cpv"),
        buyer=d.get("buyer"),
        value_min=_first(d, "value_min", "valueMin"),
        value_max=_first(d, "value_max", "valueMax"),
        sources=list(d.get("sources") or []),
    )


def _expectation_from(d: dict[str, Any] | None) -> SearchExpectation | None:
    if d is None:
        return None
    return SearchExpectation(
        min_results=_first(d, "min_results", "minResults"),
        max_results=_first(d, "max_results", "maxResults"),
        must_include_sources=list(
            _first(d, "must_include_sources", "mustIncludeSources") or []
        ),
        must_exclude_sources=list(
            _first(d, "must_exclude_sources", "mustExcludeSources") or []
        ),
        every_card_has_cpv=_first(d, "every_card_has_cpv", "everyCardHasCpv"),
        cpv_present_for_source=_first(
            d, "cpv_present_for_source", "cpvPresentForSource"
        ),
    )


def _open_from(d: dict[str, Any] | None) -> OpenTender | None:
    if d is None:
        return None
    return OpenTender(
        result_index=int(_first(d, "result_index", "resultIndex") or 0),
        cpv_field_present=_first(d, "cpv_field_present", "cpvFieldPresent"),
        title_non_empty=_first(d, "title_non_empty", "titleNonEmpty"),
    )


def load_specs_from_yaml(path: str | Path) -> list[CheckSpec]:
    """Load a list of specs from a YAML file. Accepts a single check or a
    list. PyYAML is in the bot's dev-extras (small, well-known); we import
    lazily so the pure unit tests don't pull it in."""
    import yaml  # noqa: PLC0415 - lazy

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    if isinstance(raw, dict):
        raw = [raw]
    specs: list[CheckSpec] = []
    for entry in raw:
        outcome_raw = (
            entry.get("expected_outcome") or entry.get("expectedOutcome") or "PASS"
        )
        specs.append(
            CheckSpec(
                name=entry["name"],
                rationale=entry.get("rationale", ""),
                expected_outcome=CheckOutcome(outcome_raw.upper()),
                filter=_draft_from(entry.get("filter")),
                search=_expectation_from(entry.get("search")),
                open=_open_from(entry.get("open")),
                tags=list(entry.get("tags") or []),
            )
        )
    return specs
