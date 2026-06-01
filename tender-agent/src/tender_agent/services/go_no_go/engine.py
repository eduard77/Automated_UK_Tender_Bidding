"""Top-level entrypoint for the go/no-go warning engine.

`assess_tender(db, tender)` builds the full output shape defined by the
spec (see `docs/bid-writing/go-no-go.yaml`, `output.schema`). It is a thin
orchestrator over four building blocks:

    reconciliation.reconcile_vault_against_tender
        -> vault evidence vs mandatory requirements
    warnings.detect_red_warnings
        -> the four red warnings + missing_info, using the reconciliation
    scoring.score_pillars
        -> advisory pillar scores + band + confidence
    self_cert.build_self_certification_questions
        -> the three client questions, qualifies pre-filled

NEVER blocks. Always returns warnings + a recommendation that the client
may override. NO LLM call.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.models import Tender, TenderBrief
from tender_agent.services.go_no_go.reconciliation import (
    ReconciliationResult,
    reconcile_vault_against_tender,
)
from tender_agent.services.go_no_go.scoring import AdvisoryRating, score_pillars
from tender_agent.services.go_no_go.self_cert import (
    build_self_certification_questions,
)
from tender_agent.services.go_no_go.warnings import (
    MissingInfo,
    RedWarning,
    detect_red_warnings,
)


class BriefNotReady(Exception):  # noqa: N818 — domain-condition name, not an exception-suffix style
    """Raised when a go-no-go is requested on a tender that has no
    complete brief yet. The API maps this to a clean 'generate a brief
    first' state — never a 500."""


@dataclass
class GoNoGoResult:
    """The complete output shape — see `output.schema` in the YAML."""

    tender_id: int
    brief_id: int
    rating: AdvisoryRating
    red_warnings: list[RedWarning] = field(default_factory=list)
    missing_info: list[MissingInfo] = field(default_factory=list)
    self_certification: list = field(default_factory=list)
    reconciliation: ReconciliationResult | None = None
    working_days_remaining: int | None = None

    def to_dict(self) -> dict[str, Any]:
        rating_dict = self.rating.to_dict()
        return {
            "tender_id": self.tender_id,
            "brief_id": self.brief_id,
            **rating_dict,
            "red_warnings": [w.to_dict() for w in self.red_warnings],
            "missing_info": [m.to_dict() for m in self.missing_info],
            "self_certification": [q.to_dict() for q in self.self_certification],
            "working_days_remaining": self.working_days_remaining,
            "reconciliation": (
                self.reconciliation.to_dict() if self.reconciliation else None
            ),
        }


def assess_tender(
    db: Session,
    tender: Tender,
    *,
    org_id: int = 1,
    today: date | None = None,
) -> GoNoGoResult:
    """Run the full assessment for a tender. Raises BriefNotReady if the
    tender has no complete brief yet — the API converts that to a state
    the dashboard can show 'generate a brief first' against."""
    brief = _latest_complete_brief(db, tender.id)
    if brief is None:
        raise BriefNotReady(f"no_complete_brief_for_tender:{tender.id}")

    recon = reconcile_vault_against_tender(
        db, tender=tender, brief=brief, org_id=org_id, today=today
    )
    warnings = detect_red_warnings(
        tender=tender, brief=brief, reconciliation=recon, today=today
    )
    rating = score_pillars(
        brief=brief, has_red_warning=bool(warnings.red_warnings)
    )
    questions = build_self_certification_questions(warnings.red_warnings)

    return GoNoGoResult(
        tender_id=tender.id,
        brief_id=brief.id,
        rating=rating,
        red_warnings=warnings.red_warnings,
        missing_info=warnings.missing_info,
        self_certification=questions,
        reconciliation=recon,
        working_days_remaining=warnings.working_days_remaining,
    )


def _latest_complete_brief(db: Session, tender_id: int) -> TenderBrief | None:
    return db.execute(
        select(TenderBrief)
        .where(TenderBrief.tender_id == tender_id)
        .where(TenderBrief.status == "complete")
        .order_by(TenderBrief.created_at.desc())
    ).scalars().first()
