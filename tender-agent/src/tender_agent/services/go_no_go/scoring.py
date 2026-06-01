"""Advisory pillar scoring + band mapping.

The spec defines three pillars (strategic_fit, winnability, deliverability)
weighted 25/35/40. Each pillar has 4-5 criteria; each criterion is scored
0/1/2 (or null=unknown). The weighted total maps to a band:
    go          weighted_total >= 70% AND no pillar below 50% AND no red warning
    conditional 50-69%, OR a single resolvable weakness
    no_go       < 50%, OR any pillar below floor, OR a red warning

Confidence drops as unknowns rise (low if >30% unknown OR no pricing
history at all).

We compute as much as we can DETERMINISTICALLY from the brief; criteria we
can't compute remain `unknown` (counted against confidence, not the score).
LLM scoring is allowed by the spec but optional — this module does not call
an LLM.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from tender_agent.models import TenderBrief

Band = Literal["go", "conditional", "no_go"]
Confidence = Literal["high", "medium", "low"]

PILLAR_WEIGHTS = {
    "strategic_fit": 25,
    "winnability": 35,
    "deliverability": 40,
}
PILLAR_FLOOR_PCT = 50.0


@dataclass
class CriterionScore:
    criterion: str
    score: int | None  # 0 / 1 / 2 / None=unknown
    reason: str
    grounded_in: list[str] = field(default_factory=list)


@dataclass
class PillarScore:
    pillar: str
    score_pct: float | None  # None if every criterion was unknown
    unknown_count: int
    total_count: int
    criteria: list[CriterionScore]


@dataclass
class AdvisoryRating:
    recommendation: Band
    confidence: Confidence
    headline: str
    weighted_total_pct: float | None
    pillar_scores: dict[str, float | None]
    pillars: list[PillarScore]

    def to_dict(self) -> dict[str, Any]:
        return {
            "recommendation": self.recommendation,
            "confidence": self.confidence,
            "headline": self.headline,
            "weighted_total_pct": self.weighted_total_pct,
            "pillar_scores": self.pillar_scores,
            "pillars": [
                {
                    "pillar": p.pillar,
                    "score_pct": p.score_pct,
                    "unknown_count": p.unknown_count,
                    "total_count": p.total_count,
                    "criteria": [
                        {
                            "criterion": c.criterion,
                            "score": c.score,
                            "reason": c.reason,
                            "grounded_in": c.grounded_in,
                        }
                        for c in p.criteria
                    ],
                }
                for p in self.pillars
            ],
        }


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------


def score_pillars(
    *,
    brief: TenderBrief,
    has_red_warning: bool,
) -> AdvisoryRating:
    """Deterministic scoring from the brief. Where the brief has signal we
    use it; the rest stay `unknown` (drives confidence down)."""
    body = brief.brief_json or {}
    brief_rec = (body.get("recommendation") or "").lower()

    pillars = [
        _score_strategic_fit(body, brief_rec),
        _score_winnability(body, brief_rec),
        _score_deliverability(body, brief_rec),
    ]

    pillar_score_map: dict[str, float | None] = {
        p.pillar: p.score_pct for p in pillars
    }

    # Weighted total — only over the pillars that have at least one scored
    # criterion. If every pillar is fully unknown, weighted_total is None.
    weighted_total = _weighted_total(pillars)

    band = _band(
        weighted_total=weighted_total,
        pillars=pillars,
        has_red_warning=has_red_warning,
    )
    confidence = _confidence(pillars, has_pricing_history=False)
    headline = _headline(band, brief_rec, has_red_warning, pillars)

    return AdvisoryRating(
        recommendation=band,
        confidence=confidence,
        headline=headline,
        weighted_total_pct=weighted_total,
        pillar_scores=pillar_score_map,
        pillars=pillars,
    )


# ---------------------------------------------------------------------------
# Per-pillar scorers (deterministic; defaults to unknown when we can't tell)
# ---------------------------------------------------------------------------


def _score_strategic_fit(body: dict, brief_rec: str) -> PillarScore:
    criteria: list[CriterionScore] = []
    # aligns_with_strategy — surrogate signal: brief is positive AND has a
    # scope_summary present.
    scope = body.get("scope_summary")
    if isinstance(scope, str) and scope.strip():
        if brief_rec == "bid":
            criteria.append(
                CriterionScore(
                    "aligns_with_strategy", 2,
                    "Brief recommends bid; scope is articulated.",
                    ["brief.recommendation", "brief.scope_summary"],
                )
            )
        elif brief_rec == "conditional":
            criteria.append(
                CriterionScore(
                    "aligns_with_strategy", 1,
                    "Brief is conditional; alignment is partial.",
                    ["brief.recommendation"],
                )
            )
        elif brief_rec == "no_bid":
            criteria.append(
                CriterionScore(
                    "aligns_with_strategy", 0,
                    "Brief recommends no-bid; alignment looks weak.",
                    ["brief.recommendation"],
                )
            )
        else:
            criteria.append(
                CriterionScore(
                    "aligns_with_strategy", None,
                    "No recommendation in brief.",
                )
            )
    else:
        criteria.append(
            CriterionScore(
                "aligns_with_strategy", None,
                "Brief lacks a scope summary.",
            )
        )

    # The other criteria depend on signals we don't have yet (tenant
    # capabilities, current pipeline, win probability model). Mark unknown.
    for name, reason in (
        ("wants_this_buyer", "No buyer-relationship signal in current data."),
        ("resource_headroom", "Pipeline data is outside the platform."),
        ("worth_the_bid_cost", "Win-probability model not available."),
    ):
        criteria.append(CriterionScore(name, None, reason))

    return _bundle_pillar("strategic_fit", criteria)


def _score_winnability(body: dict, brief_rec: str) -> PillarScore:
    criteria: list[CriterionScore] = []
    # competitive_position — if mandatory_requirements is empty, we're not
    # being squeezed by qualification gates; that's weak signal so still
    # mark unknown unless brief flags incumbent.
    risks = body.get("key_risks") or []
    incumbent_mentioned = any(
        isinstance(r, dict)
        and "incumbent" in (str(r.get("title", "")) + str(r.get("detail", ""))).lower()
        for r in risks
    )
    if incumbent_mentioned:
        criteria.append(
            CriterionScore(
                "competitive_position", 0,
                "Brief flags incumbent risk.",
                ["brief.key_risks"],
            )
        )
    else:
        criteria.append(
            CriterionScore(
                "competitive_position", None,
                "Competitive landscape not modelled.",
            )
        )

    # can_produce_a_compelling_bid — proxy from brief.recommendation.
    if brief_rec == "bid":
        criteria.append(
            CriterionScore(
                "can_produce_a_compelling_bid", 2,
                "Brief recommends bid; assumes vault depth.",
                ["brief.recommendation"],
            )
        )
    elif brief_rec == "conditional":
        criteria.append(
            CriterionScore(
                "can_produce_a_compelling_bid", 1,
                "Brief is conditional.",
                ["brief.recommendation"],
            )
        )
    elif brief_rec == "no_bid":
        criteria.append(
            CriterionScore(
                "can_produce_a_compelling_bid", 0,
                "Brief recommends no-bid.",
                ["brief.recommendation"],
            )
        )
    else:
        criteria.append(
            CriterionScore("can_produce_a_compelling_bid", None, "No recommendation.")
        )

    for name, reason in (
        ("buyer_relationship", "No buyer-relationship history modelled."),
        ("affordable_compliant_solution", "Pricing-affordability model not available."),
        ("track_record_here", "No comparable-bid history available."),
    ):
        criteria.append(CriterionScore(name, None, reason))

    return _bundle_pillar("winnability", criteria)


def _score_deliverability(body: dict, brief_rec: str) -> PillarScore:
    criteria: list[CriterionScore] = []
    # resources_in_place — proxy from confidence + recommendation.
    confidence = (body.get("confidence") or "").lower()
    if brief_rec == "bid" and confidence == "high":
        criteria.append(
            CriterionScore(
                "resources_in_place", 2,
                "Brief is high-confidence bid.",
                ["brief.recommendation", "brief.confidence"],
            )
        )
    elif brief_rec == "bid":
        criteria.append(
            CriterionScore(
                "resources_in_place", 1,
                "Brief recommends bid but confidence is not high.",
                ["brief.recommendation", "brief.confidence"],
            )
        )
    elif brief_rec == "no_bid":
        criteria.append(
            CriterionScore(
                "resources_in_place", 0,
                "Brief recommends no-bid.",
                ["brief.recommendation"],
            )
        )
    else:
        criteria.append(
            CriterionScore("resources_in_place", None, "Unclear from brief.")
        )

    for name, reason in (
        ("resources_obtainable", "Mobilisation window not modelled."),
        ("relevant_experience", "Case-study match not run here."),
        (
            "deliver_to_standard_and_budget_at_profit",
            "Cost-base + pricing model not available.",
        ),
    ):
        criteria.append(CriterionScore(name, None, reason))

    return _bundle_pillar("deliverability", criteria)


def _bundle_pillar(name: str, criteria: list[CriterionScore]) -> PillarScore:
    scored = [c for c in criteria if c.score is not None]
    if not scored:
        return PillarScore(
            pillar=name,
            score_pct=None,
            unknown_count=len(criteria),
            total_count=len(criteria),
            criteria=criteria,
        )
    achievable = 2 * len(scored)
    earned = sum(c.score or 0 for c in scored)
    pct = (earned / achievable) * 100.0 if achievable else None
    return PillarScore(
        pillar=name,
        score_pct=pct,
        unknown_count=len(criteria) - len(scored),
        total_count=len(criteria),
        criteria=criteria,
    )


def _weighted_total(pillars: list[PillarScore]) -> float | None:
    weighted = 0.0
    total_weight = 0.0
    for p in pillars:
        if p.score_pct is None:
            continue
        weight = PILLAR_WEIGHTS.get(p.pillar, 0)
        weighted += p.score_pct * weight
        total_weight += weight
    if total_weight == 0:
        return None
    return weighted / total_weight


def _band(
    *,
    weighted_total: float | None,
    pillars: list[PillarScore],
    has_red_warning: bool,
) -> Band:
    if has_red_warning:
        return "no_go"
    if weighted_total is None:
        return "conditional"
    any_pillar_below_floor = any(
        p.score_pct is not None and p.score_pct < PILLAR_FLOOR_PCT
        for p in pillars
    )
    if weighted_total < 50.0 or any_pillar_below_floor:
        return "no_go"
    if weighted_total < 70.0:
        return "conditional"
    return "go"


def _confidence(
    pillars: list[PillarScore], *, has_pricing_history: bool
) -> Confidence:
    total = sum(p.total_count for p in pillars)
    unknown = sum(p.unknown_count for p in pillars)
    if total == 0:
        return "low"
    unknown_ratio = unknown / total
    if not has_pricing_history:
        return "low"
    if unknown_ratio > 0.3:
        return "low"
    if unknown_ratio > 0.1:
        return "medium"
    return "high"


def _headline(
    band: Band,
    brief_rec: str,
    has_red_warning: bool,
    pillars: list[PillarScore],
) -> str:
    if has_red_warning:
        return "No-go (advisory): a red warning is open. Review before paying."
    if band == "go":
        rec = brief_rec or "bid"
        return f"Go (advisory): the brief recommends '{rec}' and pillars look healthy."
    if band == "conditional":
        return (
            "Conditional (advisory): proceed with care — review the points to resolve."
        )
    return "No-go (advisory): weighted total below threshold or pillar below floor."
