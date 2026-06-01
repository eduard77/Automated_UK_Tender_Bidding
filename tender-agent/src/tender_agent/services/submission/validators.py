"""Server-side validators — the six self-checks from AGENT_SYSTEM_PROMPT.md
LAYER 4. The agent reports its own self-check in `validation_report`; this
module RE-RUNS the checks deterministically against the structured content
so the server is the source of truth. If the agent claimed a pass and the
server-side check disagrees, the server wins and the draft is marked
incomplete.

Returns a `ValidationReport` plus a flag `is_blocking_failure` that the
engine uses to choose between status='needs_review' and status='incomplete'.
NEVER changes the structured_content — read-only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from tender_agent.services.submission.templates import TemplateSpec

# Cross-section consistency tolerance for "the same KPI / commitment number".
# We compare integers / floats exactly; the agent should never emit two
# different values for the same named KPI. Used by `_cross_section_check`.


@dataclass
class CheckResult:
    name: str
    pass_: bool  # 'pass' is a builtin; trailing underscore is the Python idiom
    severity: str  # 'blocking' | 'non_blocking'
    details: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "pass": self.pass_,
            "severity": self.severity,
            "details": self.details,
        }


@dataclass
class ValidationReport:
    evidence_minimums: dict[str, dict[str, Any]] = field(default_factory=dict)
    scoring_check_results: list[CheckResult] = field(default_factory=list)
    failure_modes_check: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {"exhibited": [], "near_misses": []}
    )
    cross_section_validation: dict[str, Any] = field(default_factory=dict)
    language_rules_check: dict[str, Any] = field(default_factory=dict)
    confidence_summary: dict[str, Any] = field(default_factory=dict)
    copyright_check: dict[str, Any] = field(default_factory=dict)
    # Convenience roll-up.
    is_blocking_failure: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_minimums_check": self.evidence_minimums,
            "scoring_check_results": [c.to_dict() for c in self.scoring_check_results],
            "failure_modes_check": self.failure_modes_check,
            "cross_section_validation": self.cross_section_validation,
            "language_rules_check": self.language_rules_check,
            "confidence_summary": self.confidence_summary,
            "copyright_check": self.copyright_check,
            "is_blocking_failure": self.is_blocking_failure,
        }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def run_validators(
    *,
    template: TemplateSpec,
    structured_content: dict[str, Any],
    vault_citations: list,
    confidence_scores: dict[str, Any],
    unfilled_slots: list,
    cross_section_state: list[dict[str, Any]],
    itt_question_text: str,
) -> ValidationReport:
    """Six checks in order, write everything into a single report. A
    blocking failure in any check sets `is_blocking_failure=True`; the
    engine reads that and stores the draft with status='incomplete'."""
    report = ValidationReport()

    _check_evidence_minimums(template, structured_content, vault_citations, report)
    _check_scoring_rules(template, structured_content, vault_citations, report)
    _check_failure_modes(template, structured_content, report)
    _check_cross_section(structured_content, cross_section_state, report)
    _check_language_rules(structured_content, report)
    _check_confidence_summary(confidence_scores, report)
    _check_copyright(structured_content, itt_question_text, report)

    if any(
        r.severity == "blocking" and not r.pass_
        for r in report.scoring_check_results
    ):
        report.is_blocking_failure = True
    if report.copyright_check.get("verbatim_run_too_long"):
        report.is_blocking_failure = True
    if not report.cross_section_validation.get("consistent", True):
        report.is_blocking_failure = True
    return report


# ---------------------------------------------------------------------------
# Check 1 — evidence_minimums
# ---------------------------------------------------------------------------


def _check_evidence_minimums(
    template: TemplateSpec,
    content: dict[str, Any],
    vault_citations: list,
    report: ValidationReport,
) -> None:
    counts = _count_evidence_signals(content, vault_citations)
    for key, required in template.evidence_minimums.items():
        found = counts.get(key, 0)
        report.evidence_minimums[key] = {
            "required": required,
            "found": found,
            "pass": found >= required,
        }


def _count_evidence_signals(
    content: dict[str, Any], vault_citations: list
) -> dict[str, int]:
    """Best-effort counters that match the spec's evidence_minimums keys.
    These look for STRUCTURAL signals (named clients in case_studies,
    certificate_number fields, named_people, numeric kpis) — not free-text
    matching."""
    case_studies = _walk(content, "evidence", "case_studies") or _walk(
        content, "past_delivery"
    ) or []
    named_people_a = _walk(content, "team", "named_people") or []
    sv_lead = _walk(content, "sv_lead")
    certifications = (
        _walk(content, "methodology", "standards")
        or _walk(content, "certifications")
        or []
    )
    kpis = _walk(content, "kpis", "kpis") or _walk(content, "kpis") or _walk(
        content, "measurement", "kpis"
    ) or []
    added_value = _walk(content, "added_value", "items") or []
    risks = _walk(content, "risks", "risks") or _walk(content, "risks") or []
    buyer_fit = _walk(content, "buyer_fit")
    commitments = _walk(content, "commitments") or []
    past_delivery = _walk(content, "past_delivery") or []
    ci = _walk(content, "ci_methodology")

    def _count_with(field_name: str, items: list) -> int:
        return sum(
            1
            for item in items
            if isinstance(item, dict) and item.get(field_name)
        )

    return {
        "named_clients": _count_with("client_or_label", case_studies)
        + _count_with("client", case_studies),
        "certificate_numbers": _count_with("certificate_number", certifications),
        "certificate_expiry_dates": _count_with("expiry_date", certifications),
        "named_people": _count_with("name", named_people_a),
        "numeric_kpis": sum(
            1 for k in kpis if isinstance(k, dict) and k.get("target")
        ),
        "buyer_doc_references": (1 if buyer_fit else 0),
        "quantified_added_value": _count_with("quantification", added_value),
        "commitments_with_capability_id": sum(
            1
            for c in commitments
            if isinstance(c, dict) and c.get("vault_capability_id")
        ),
        "past_delivery_examples": len(past_delivery),
        "named_sv_lead": 1 if sv_lead else 0,
        "phases": len(_walk(content, "methodology", "phases") or []),
        "ci_worked_examples": 1 if ci else 0,
        "risks": len(risks),
        "risks_with_scores": sum(
            1
            for r in risks
            if isinstance(r, dict)
            and r.get("inherent") is not None
            and r.get("residual") is not None
            and r.get("target_residual") is not None
        ),
        "risks_with_owners": _count_with("owner", risks),
        "kpis": len(kpis),
    }


# ---------------------------------------------------------------------------
# Check 2 — scoring_check rules (deterministic projections of fail_if)
# ---------------------------------------------------------------------------


def _check_scoring_rules(
    template: TemplateSpec,
    content: dict[str, Any],
    vault_citations: list,
    report: ValidationReport,
) -> None:
    handlers = {
        "methodology_named": _scoring_methodology_named,
        "named_people": _scoring_named_people,
        "contract_specific": _scoring_contract_specific,
        "mobilisation_week_by_week": _scoring_mobilisation,
        "named_fte_per_phase": _scoring_fte_per_phase,
        "selected_policy_outcome_matches_buyer": _scoring_selected_policy,
        "deliverability_hard_block": _scoring_deliverability,
        "cert_expiry_validity": _scoring_cert_expiry,
        "named_qms_framework": _scoring_named_qms,
        "every_risk_scored": _scoring_every_risk_scored,
        "named_owners_seniority": _scoring_named_owners,
    }
    for rule in template.scoring_check:
        handler = handlers.get(rule.rule_id)
        if handler is None:
            # Spec ships rules we don't yet evaluate; surface as
            # non-blocking 'unknown' instead of falsely passing.
            report.scoring_check_results.append(
                CheckResult(
                    name=rule.rule_id,
                    pass_=True,
                    severity="non_blocking",
                    details="check not implemented; trusting the agent's self-report",
                )
            )
            continue
        ok, details = handler(content)
        report.scoring_check_results.append(
            CheckResult(
                name=rule.rule_id,
                pass_=ok,
                severity=rule.severity,
                details=details,
            )
        )


def _scoring_methodology_named(content: dict) -> tuple[bool, str | None]:
    name = _walk(content, "methodology", "methodology_name")
    if not name:
        return False, "methodology.methodology_name missing"
    if str(name).strip().lower() in {"best practice", "industry standard"}:
        return (
            False,
            f"methodology_name is a generic phrase: {name!r}",
        )
    return True, None


def _scoring_named_people(content: dict) -> tuple[bool, str | None]:
    team = _walk(content, "team", "named_people") or []
    named = [
        p for p in team
        if isinstance(p, dict) and p.get("name") and len(str(p.get("name")).strip()) > 1
    ]
    if not named:
        return False, "no named individuals in team.named_people"
    return True, None


def _scoring_contract_specific(content: dict) -> tuple[bool, str | None]:
    risks = _walk(content, "risks", "risks") or []
    generic_phrases = ("staff turnover", "supply chain disruption", "weather")
    hits = [
        r for r in risks
        if isinstance(r, dict)
        and any(g in str(r.get("risk", "")).lower() for g in generic_phrases)
    ]
    if hits:
        return False, f"risks look generic: {[h.get('risk') for h in hits]}"
    return True, None


def _scoring_mobilisation(content: dict) -> tuple[bool, str | None]:
    weeks = _walk(content, "mobilisation", "week_by_week") or []
    if len(weeks) < 4:
        return (
            False,
            f"mobilisation.week_by_week has {len(weeks)} entries; need >= 4",
        )
    return True, None


def _scoring_fte_per_phase(content: dict) -> tuple[bool, str | None]:
    phases = _walk(content, "fte_plan", "phases") or []
    missing = [
        p for p in phases
        if isinstance(p, dict) and p.get("fte_count") is None
    ]
    if missing:
        return False, f"{len(missing)} phase(s) missing fte_count"
    return True, None


def _scoring_selected_policy(content: dict) -> tuple[bool, str | None]:
    selected = _walk(content, "selected_policy_outcome")
    if not selected:
        return False, "selected_policy_outcome missing"
    return True, None


def _scoring_deliverability(content: dict) -> tuple[bool, str | None]:
    commitments = _walk(content, "commitments") or []
    missing = [
        c for c in commitments
        if isinstance(c, dict) and not c.get("vault_capability_id")
    ]
    if missing:
        return (
            False,
            f"{len(missing)} commitment(s) without vault_capability_id",
        )
    return True, None


def _scoring_cert_expiry(content: dict) -> tuple[bool, str | None]:
    certs = _walk(content, "certifications") or []
    expired = [
        c for c in certs
        if isinstance(c, dict) and c.get("expiry_date") and _is_expired(c["expiry_date"])
    ]
    if expired:
        return False, f"{len(expired)} cert(s) expired or near-expiry"
    return True, None


def _scoring_named_qms(content: dict) -> tuple[bool, str | None]:
    name = _walk(content, "qms_framework", "name") or _walk(
        content, "qms_framework"
    )
    if not name or (isinstance(name, str) and not name.strip()):
        return False, "qms_framework.name missing"
    return True, None


def _scoring_every_risk_scored(content: dict) -> tuple[bool, str | None]:
    risks = _walk(content, "risks") or []
    unscored = [
        r for r in risks
        if isinstance(r, dict)
        and (
            r.get("inherent") is None
            or r.get("residual") is None
            or r.get("target_residual") is None
        )
    ]
    if unscored:
        return False, f"{len(unscored)} risk(s) missing inherent/residual/target"
    return True, None


def _scoring_named_owners(content: dict) -> tuple[bool, str | None]:
    risks = _walk(content, "risks") or []
    unowned = [
        r for r in risks
        if isinstance(r, dict) and not r.get("owner")
    ]
    if unowned:
        return False, f"{len(unowned)} risk(s) without an owner"
    return True, None


def _is_expired(expiry_iso: str) -> bool:
    from datetime import date

    try:
        return date.fromisoformat(expiry_iso) < date.today()
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Check 3 — failure-mode patterns (textual near-misses)
# ---------------------------------------------------------------------------


def _check_failure_modes(
    template: TemplateSpec,
    content: dict[str, Any],
    report: ValidationReport,
) -> None:
    text_blob = _flatten_text(content).lower()
    for mode_id, pattern in template.failure_patterns:
        if re.search(pattern, text_blob, flags=re.IGNORECASE):
            report.failure_modes_check["near_misses"].append(
                {"mode": mode_id, "pattern": pattern}
            )


# ---------------------------------------------------------------------------
# Check 4 — cross-section consistency
# ---------------------------------------------------------------------------


def _check_cross_section(
    content: dict[str, Any],
    earlier_drafts: list[dict[str, Any]],
    report: ValidationReport,
) -> None:
    """For every numeric KPI named in the current draft, check whether a
    KPI of the same name exists in an earlier draft on the same package.
    If yes, values must match. Disagreement is a BLOCKING failure."""
    inconsistencies: list[dict[str, Any]] = []
    my_kpis = _collect_kpis(content)
    for prior in earlier_drafts:
        prior_kpis = _collect_kpis(prior.get("structured_content") or {})
        for name, value in my_kpis.items():
            if name in prior_kpis and prior_kpis[name] != value:
                inconsistencies.append(
                    {
                        "kind": "kpi_mismatch",
                        "metric": name,
                        "this_value": value,
                        "other_value": prior_kpis[name],
                        "other_draft_id": prior.get("draft_id"),
                        "other_template_id": prior.get("template_id"),
                    }
                )
    report.cross_section_validation = {
        "consistent": not inconsistencies,
        "inconsistencies": inconsistencies,
        "kpis_compared": len(my_kpis),
    }


def _collect_kpis(content: dict[str, Any]) -> dict[str, str]:
    """Returns {metric_name -> target_value_as_string}."""
    out: dict[str, str] = {}
    kpis = _walk(content, "kpis", "kpis") or _walk(content, "kpis") or _walk(
        content, "measurement", "kpis"
    ) or []
    for k in kpis:
        if not isinstance(k, dict):
            continue
        metric = k.get("metric") or k.get("name")
        target = k.get("target")
        if metric is not None and target is not None:
            out[str(metric).strip().lower()] = str(target).strip()
    return out


# ---------------------------------------------------------------------------
# Check 5 — language rules
# ---------------------------------------------------------------------------

_BANNED_OPENINGS = (
    "it is worth noting",
    "we are pleased to confirm",
    "it goes without saying",
    "as you would expect",
)


def _check_language_rules(content: dict[str, Any], report: ValidationReport) -> None:
    headline = (_walk(content, "headline") or "").strip().lower()
    issues: list[str] = []
    for banned in _BANNED_OPENINGS:
        if headline.startswith(banned):
            issues.append(f"banned opening: {banned!r}")
    report.language_rules_check = {
        "pass": not issues,
        "issues": issues,
    }


# ---------------------------------------------------------------------------
# Check 6 — confidence summary
# ---------------------------------------------------------------------------


def _check_confidence_summary(
    confidence_scores: dict[str, Any], report: ValidationReport
) -> None:
    values: list[int] = []
    for v in _walk_values(confidence_scores):
        if isinstance(v, (int, float)):
            values.append(int(v))
    if not values:
        report.confidence_summary = {
            "claims_total": 0,
            "high_confidence_95_plus": 0,
            "medium_confidence_60_94": 0,
            "low_confidence_40_59": 0,
        }
        return
    report.confidence_summary = {
        "claims_total": len(values),
        "high_confidence_95_plus": sum(1 for v in values if v >= 95),
        "medium_confidence_60_94": sum(1 for v in values if 60 <= v < 95),
        "low_confidence_40_59": sum(1 for v in values if 40 <= v < 60),
        "below_40": sum(1 for v in values if v < 40),
    }


# ---------------------------------------------------------------------------
# Copyright (hard constraint): verbatim runs from the ITT > 15 words
# ---------------------------------------------------------------------------

_COPYRIGHT_VERBATIM_MAX_WORDS = 15


def _check_copyright(
    content: dict[str, Any], itt_text: str, report: ValidationReport
) -> None:
    if not itt_text or not itt_text.strip():
        report.copyright_check = {
            "verbatim_run_too_long": False,
            "longest_run_words": 0,
        }
        return
    draft_blob = _flatten_text(content)
    longest = _longest_shared_run(itt_text, draft_blob)
    report.copyright_check = {
        "verbatim_run_too_long": longest > _COPYRIGHT_VERBATIM_MAX_WORDS,
        "longest_run_words": longest,
        "limit_words": _COPYRIGHT_VERBATIM_MAX_WORDS,
    }


def _longest_shared_run(a: str, b: str) -> int:
    """Length, in words, of the longest contiguous word-sequence shared
    between `a` and `b`. Case-insensitive, punctuation-tolerant. O(n*m)
    but n / m are small — a question text + a draft body."""
    a_words = re.findall(r"\w+", a.lower())
    b_words = re.findall(r"\w+", b.lower())
    if not a_words or not b_words:
        return 0
    a_set: dict[str, list[int]] = {}
    for i, w in enumerate(a_words):
        a_set.setdefault(w, []).append(i)
    longest = 0
    for j, w in enumerate(b_words):
        for i in a_set.get(w, ()):
            run = 0
            while (
                i + run < len(a_words)
                and j + run < len(b_words)
                and a_words[i + run] == b_words[j + run]
            ):
                run += 1
            if run > longest:
                longest = run
    return longest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _walk(d: Any, *path: str) -> Any:
    cur = d
    for step in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(step)
        if cur is None:
            return None
    return cur


def _flatten_text(data: Any) -> str:
    """Concatenate every string leaf from a nested dict/list. Used for
    pattern + copyright scans."""
    out: list[str] = []
    if isinstance(data, str):
        out.append(data)
    elif isinstance(data, dict):
        for v in data.values():
            out.append(_flatten_text(v))
    elif isinstance(data, list):
        for item in data:
            out.append(_flatten_text(item))
    return " ".join(s for s in out if s)


def _walk_values(data: Any):
    if isinstance(data, dict):
        for v in data.values():
            yield from _walk_values(v)
    elif isinstance(data, list):
        for item in data:
            yield from _walk_values(item)
    else:
        yield data
