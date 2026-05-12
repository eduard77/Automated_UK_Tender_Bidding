#!/usr/bin/env python3
"""Validate the requirements extractor against real tenders.

Runs extraction on one or more tenders (or simulates it with --dry-run) and
produces a markdown report covering:

1. Schema conformance — does the extractor's output match the structure in
   PROJECT.md §5.3 (required keys present, enums valid, lists are lists).
2. Requirement grounding — every mandatory_requirement.requirement string is
   either >= 30% lowercase token-overlap with the source text or marked
   `"confidence": "low"`. Anything failing both is flagged as
   "potentially hallucinated".
3. Number grounding — every numeric literal in the extracted output (amounts,
   percentages, dates) must appear in the source text. Numbers in extracted
   output that don't appear in the source are flagged as "potentially
   hallucinated".

Usage:
    python scripts/validate_extractor.py --tender-id 42 --output /tmp/report.md
    python scripts/validate_extractor.py --recent 10 --output report.md
    python scripts/validate_extractor.py --all-without-brief --dry-run

A passing run shows <2% potentially-hallucinated claims across >= 20 real
tenders (target threshold for Phase 2 acceptance).
"""
from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

# Make `tender_agent` importable when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tender_agent.db import SessionLocal  # noqa: E402
from tender_agent.models import Tender, TenderRequirements  # noqa: E402
from tender_agent.services.document_downloader import aggregate_text  # noqa: E402
from tender_agent.services.requirements_extractor import extract_requirements  # noqa: E402

# ---------------------------------------------------------------------------
# Schema (mirrors PROJECT.md §5.3 + the prompt's JSON contract).
# ---------------------------------------------------------------------------

REQUIRED_TOP_LEVEL = {
    "summary",
    "evaluation_criteria",
    "mandatory_requirements",
    "desired_requirements",
    "documents_required",
    "questions_to_answer",
    "risk_flags",
    "estimated_effort_days",
    "recommendation",
    "recommendation_reason",
}
RECOMMENDATION_ENUM = {"pursue", "decline", "review"}
CONFIDENCE_ENUM = {"high", "medium", "low"}

# Heuristics
TOKEN_OVERLAP_THRESHOLD = 0.30  # fraction of requirement tokens that must appear in source
TOKEN_RE = re.compile(r"[a-z0-9]{3,}")
NUMBER_RE = re.compile(
    r"""
    (?<![\w.])              # not preceded by word char or dot
    (?:£|\$|€)?\s*          # optional currency prefix
    \d{1,3}(?:,\d{3})+      # 1,234,567
    (?:\.\d+)?              # optional decimal
    |
    (?<![\w.])\d+(?:\.\d+)?%?  # plain integer or decimal, possibly %
    """,
    re.VERBOSE,
)


# ---------------------------------------------------------------------------
# Results model
# ---------------------------------------------------------------------------


@dataclass
class TenderResult:
    tender_id: int
    title: str
    schema_errors: list[str] = field(default_factory=list)
    ungrounded_requirements: list[dict] = field(default_factory=list)
    ungrounded_numbers: list[dict] = field(default_factory=list)
    total_requirements: int = 0
    total_numbers: int = 0
    skipped_reason: str | None = None

    @property
    def potentially_hallucinated_count(self) -> int:
        return len(self.ungrounded_requirements) + len(self.ungrounded_numbers)

    @property
    def total_claims(self) -> int:
        return self.total_requirements + self.total_numbers

    @property
    def hallucination_rate(self) -> float:
        return (
            self.potentially_hallucinated_count / self.total_claims
            if self.total_claims
            else 0.0
        )

    @property
    def passed(self) -> bool:
        return (
            self.skipped_reason is None
            and not self.schema_errors
            and self.hallucination_rate < 0.02
        )


# ---------------------------------------------------------------------------
# Validation primitives
# ---------------------------------------------------------------------------


def _tokens(text: str) -> set[str]:
    return set(TOKEN_RE.findall(text.lower()))


def _numbers(text: str) -> set[str]:
    """Extract normalised numeric literals from a string (commas + currency stripped)."""
    out: set[str] = set()
    for raw in NUMBER_RE.findall(text or ""):
        normalised = re.sub(r"[^\d.%]", "", raw)
        if normalised and normalised != ".":
            out.add(normalised)
    return out


def check_schema(parsed: dict) -> list[str]:
    """Return a list of schema errors (empty list = valid)."""
    errors: list[str] = []

    if not isinstance(parsed, dict):
        return [f"top-level is {type(parsed).__name__}, expected dict"]

    missing = REQUIRED_TOP_LEVEL - parsed.keys()
    if missing:
        errors.append(f"missing top-level keys: {sorted(missing)}")

    extra = parsed.keys() - REQUIRED_TOP_LEVEL
    # `model` and `extracted_at` are persisted on the DB row but not part of
    # the prompt schema; tolerate them at the top level.
    extra -= {"model", "extracted_at", "raw_response"}
    if extra:
        errors.append(f"unexpected top-level keys: {sorted(extra)}")

    rec = parsed.get("recommendation")
    if rec is not None and rec not in RECOMMENDATION_ENUM:
        errors.append(
            f"recommendation={rec!r} not in {sorted(RECOMMENDATION_ENUM)}"
        )

    for list_field in (
        "evaluation_criteria",
        "mandatory_requirements",
        "desired_requirements",
        "documents_required",
        "questions_to_answer",
        "risk_flags",
    ):
        value = parsed.get(list_field)
        if value is not None and not isinstance(value, list):
            errors.append(f"{list_field} is {type(value).__name__}, expected list")

    for item in parsed.get("mandatory_requirements") or []:
        if not isinstance(item, dict):
            errors.append(f"mandatory_requirements item is {type(item).__name__}")
            continue
        conf = item.get("confidence")
        if conf is not None and conf not in CONFIDENCE_ENUM:
            errors.append(
                f"mandatory_requirements[].confidence={conf!r} not in {sorted(CONFIDENCE_ENUM)}"
            )

    return errors


def check_requirement_grounding(
    requirements: list[dict] | None, source_text: str
) -> list[dict]:
    """Flag requirement strings that are neither well-grounded in source nor
    explicitly marked `confidence: "low"`. Returns one entry per ungrounded
    requirement; empty list if all good."""
    if not requirements:
        return []

    source_tokens = _tokens(source_text)
    out: list[dict] = []
    for i, req in enumerate(requirements):
        if not isinstance(req, dict):
            continue
        text = req.get("requirement") or ""
        confidence = req.get("confidence")
        if confidence == "low":
            continue
        req_tokens = _tokens(text)
        if not req_tokens:
            continue
        overlap = len(req_tokens & source_tokens) / len(req_tokens)
        if overlap < TOKEN_OVERLAP_THRESHOLD:
            out.append(
                {
                    "index": i,
                    "text": text,
                    "confidence": confidence,
                    "overlap": round(overlap, 3),
                }
            )
    return out


# Keys whose numeric content we deliberately ignore for grounding purposes:
# - `id` / `criterion_id` etc: synthetic identifiers the extractor creates
#   (e.g. "DRY-1"); they aren't claims about source content.
# - `estimated_effort_days`: a judgement call, not a quote from the source.
# - `weight` / `weight_pct` / `word_limit`: operator-side metadata, often
#   estimated even when the source doesn't give an exact figure.
_GROUNDING_SKIP_KEYS = {
    "id",
    "estimated_effort_days",
    "weight",
    "weight_pct",
    "word_limit",
}


def check_number_grounding(parsed: dict, source_text: str) -> list[dict]:
    """Every numeric literal that appears in the extracted output must also
    appear in the source. Returns one entry per ungrounded number."""
    source_numbers = _numbers(source_text)

    # Walk the extracted dict and collect numbers from every string value.
    extracted_numbers: list[tuple[str, str]] = []  # (path, value)

    def walk(node: Any, path: str, *, parent_key: str | None = None) -> None:
        if parent_key in _GROUNDING_SKIP_KEYS:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else k, parent_key=k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]", parent_key=parent_key)
        elif isinstance(node, str):
            for n in _numbers(node):
                extracted_numbers.append((path, n))
        elif isinstance(node, int | float):
            extracted_numbers.append((path, str(node)))

    walk(parsed, "")

    out: list[dict] = []
    for path, n in extracted_numbers:
        if n in source_numbers:
            continue
        out.append({"path": path, "value": n})
    return out


def count_numbers(parsed: dict) -> int:
    count = 0

    def walk(node: Any, *, parent_key: str | None = None) -> None:
        nonlocal count
        if parent_key in _GROUNDING_SKIP_KEYS:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, parent_key=k)
        elif isinstance(node, list):
            for v in node:
                walk(v, parent_key=parent_key)
        elif isinstance(node, str):
            count += len(_numbers(node))
        elif isinstance(node, int | float):
            count += 1

    walk(parsed)
    return count


# ---------------------------------------------------------------------------
# Tender selection
# ---------------------------------------------------------------------------


def select_tenders(
    db: Session,
    *,
    tender_id: int | None,
    recent: int | None,
    all_without_brief: bool,
) -> list[Tender]:
    if tender_id is not None:
        t = db.get(Tender, tender_id)
        return [t] if t is not None else []

    if all_without_brief:
        stmt = (
            select(Tender)
            .outerjoin(TenderRequirements, TenderRequirements.tender_id == Tender.id)
            .where(TenderRequirements.id.is_(None))
            .order_by(Tender.published_at.desc().nullslast())
        )
        return list(db.execute(stmt).scalars().all())

    if recent is not None:
        stmt = (
            select(Tender)
            .outerjoin(TenderRequirements, TenderRequirements.tender_id == Tender.id)
            .where(TenderRequirements.id.is_(None))
            .order_by(Tender.published_at.desc().nullslast())
            .limit(recent)
        )
        return list(db.execute(stmt).scalars().all())

    return []


def source_text_for(tender: Tender) -> str:
    """The 'source of truth' that extracted claims must be grounded in:
    aggregated document text + the tender description itself."""
    parts: list[str] = []
    if tender.description:
        parts.append(tender.description)
    doc_text = aggregate_text(tender.document_files or [])
    if doc_text:
        parts.append(doc_text)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Validation runner
# ---------------------------------------------------------------------------


def validate_tender(
    db: Session, tender: Tender, *, dry_run: bool
) -> TenderResult:
    result = TenderResult(tender_id=tender.id, title=tender.title or "(untitled)")
    source = source_text_for(tender)

    if not source:
        result.skipped_reason = "no description and no document text"
        return result

    if dry_run:
        # Build a synthetic, schema-conformant payload so the schema and
        # grounding checks exercise something. We deliberately copy two
        # numbers from the source so number-grounding flags zero hits, and
        # one requirement string that's >=30% token overlap with the source.
        sample_numbers = list(_numbers(source))[:2]
        first_sentence = source.split(".")[0][:200] if source else ""
        parsed = {
            "summary": "Dry-run synthetic brief — no Anthropic call made.",
            "evaluation_criteria": [],
            "mandatory_requirements": [
                {
                    "id": "DRY-1",
                    "requirement": first_sentence or "Dry-run requirement.",
                    "evidence_needed": None,
                    "confidence": "high",
                }
            ],
            "desired_requirements": [],
            "documents_required": [],
            "questions_to_answer": [],
            "risk_flags": [f"dry-run · {n}" for n in sample_numbers],
            "estimated_effort_days": 5,
            "recommendation": "review",
            "recommendation_reason": "Dry run; recommendation not meaningful.",
        }
    else:
        record = extract_requirements(db, tender)
        if record is None:
            result.skipped_reason = "extractor returned None (api error / parse failure / no key)"
            return result
        parsed = record.raw_response or {
            "summary": record.summary,
            "evaluation_criteria": record.evaluation_criteria,
            "mandatory_requirements": record.mandatory_requirements,
            "desired_requirements": record.desired_requirements,
            "documents_required": record.documents_required,
            "questions_to_answer": record.questions_to_answer,
            "risk_flags": record.risk_flags,
            "estimated_effort_days": record.estimated_effort_days,
            "recommendation": record.recommendation,
            "recommendation_reason": record.recommendation_reason,
        }

    result.schema_errors = check_schema(parsed)
    result.total_requirements = len(parsed.get("mandatory_requirements") or [])
    result.ungrounded_requirements = check_requirement_grounding(
        parsed.get("mandatory_requirements"), source
    )
    result.total_numbers = count_numbers(parsed)
    result.ungrounded_numbers = check_number_grounding(parsed, source)
    return result


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def render_report(results: list[TenderResult], *, dry_run: bool) -> str:
    lines: list[str] = []
    lines.append("# Extractor validation report")
    lines.append("")
    lines.append(f"_Generated: {datetime.now().isoformat(timespec='seconds')}_")
    lines.append(f"_Mode: {'DRY-RUN (no Anthropic calls)' if dry_run else 'LIVE'}_")
    lines.append("")

    processed = [r for r in results if r.skipped_reason is None]
    skipped = [r for r in results if r.skipped_reason is not None]
    total_claims = sum(r.total_claims for r in processed)
    total_hallucinated = sum(r.potentially_hallucinated_count for r in processed)
    overall_rate = total_hallucinated / total_claims if total_claims else 0.0
    overall_pass = (
        bool(processed)
        and overall_rate < 0.02
        and all(not r.schema_errors for r in processed)
    )

    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Tenders processed: **{len(processed)}**")
    lines.append(f"- Tenders skipped: {len(skipped)}")
    lines.append(f"- Total claims checked: {total_claims}")
    lines.append(
        f"- Potentially hallucinated claims: **{total_hallucinated}** "
        f"({overall_rate * 100:.2f}%)"
    )
    lines.append(
        f"- Schema conformance: "
        f"{sum(1 for r in processed if not r.schema_errors)}/{len(processed)} passed"
    )
    lines.append(
        f"- Overall: {'**PASS** ✅' if overall_pass else '**FAIL** ❌'} "
        f"(target: schema clean + <2% hallucination across processed tenders)"
    )
    lines.append("")

    lines.append("## Per-tender results")
    lines.append("")
    if not results:
        lines.append("_No tenders matched the selection criteria._")
        lines.append("")
    for r in results:
        lines.append(f"### #{r.tender_id} — {r.title}")
        lines.append("")
        if r.skipped_reason:
            lines.append(f"- ⏭ **Skipped:** {r.skipped_reason}")
            lines.append("")
            continue
        verdict = "✅ pass" if r.passed else "❌ fail"
        lines.append(f"- Verdict: {verdict}")
        lines.append(
            f"- Schema errors: {len(r.schema_errors)} "
            f"· requirements checked: {r.total_requirements} "
            f"· numbers checked: {r.total_numbers}"
        )
        lines.append(
            f"- Potentially hallucinated: {r.potentially_hallucinated_count} "
            f"({r.hallucination_rate * 100:.2f}%)"
        )
        if r.schema_errors:
            lines.append("")
            lines.append("**Schema errors**")
            for e in r.schema_errors:
                lines.append(f"- {e}")
        if r.ungrounded_requirements:
            lines.append("")
            lines.append("**Ungrounded requirements** (low token overlap with source)")
            for item in r.ungrounded_requirements:
                lines.append(
                    f"- [{item['overlap']:.0%} overlap, "
                    f"confidence={item['confidence']!r}] _\"{item['text'][:200]}\"_"
                )
        if r.ungrounded_numbers:
            lines.append("")
            lines.append("**Ungrounded numbers** (in extracted output but not in source)")
            for item in r.ungrounded_numbers:
                lines.append(f"- `{item['path']}` → `{item['value']}`")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    select_group = parser.add_mutually_exclusive_group(required=True)
    select_group.add_argument("--tender-id", type=int, help="extract one tender by id")
    select_group.add_argument(
        "--recent",
        type=int,
        metavar="N",
        help="extract the N most recent tenders that don't already have a brief",
    )
    select_group.add_argument(
        "--all-without-brief",
        action="store_true",
        help="extract every tender without a brief",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="write the markdown report to this path (stdout if omitted)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="don't call Anthropic; emit a synthetic extraction for schema/grounding checks",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    with SessionLocal() as db:
        tenders = select_tenders(
            db,
            tender_id=args.tender_id,
            recent=args.recent,
            all_without_brief=args.all_without_brief,
        )
        if not tenders:
            print("validate: no tenders selected (check arguments)", file=sys.stderr)
            return 2

        results = [validate_tender(db, t, dry_run=args.dry_run) for t in tenders]

    report = render_report(results, dry_run=args.dry_run)
    if args.output:
        args.output.write_text(report, encoding="utf-8")
        print(f"validate: wrote {args.output} ({len(results)} tender(s))", file=sys.stderr)
    else:
        sys.stdout.write(report)
        sys.stdout.write("\n")

    overall_pass = (
        bool(results)
        and all(r.passed or r.skipped_reason for r in results)
        and any(r.skipped_reason is None for r in results)
    )
    return 0 if overall_pass else 1


# Re-export so tests / docs can reference it
__all__ = [
    "TenderResult",
    "check_number_grounding",
    "check_requirement_grounding",
    "check_schema",
    "main",
    "render_report",
    "select_tenders",
    "validate_tender",
]


def _iter_results(results: list[TenderResult]) -> Iterator[TenderResult]:
    """Helper for downstream callers that want to consume results lazily."""
    yield from results


if __name__ == "__main__":
    raise SystemExit(main())
