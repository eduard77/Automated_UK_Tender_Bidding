"""Client self-certification questions emitter.

The spec defines exactly three questions. We emit them every time the
go-no-go is requested. The `qualifies` question is PRE-FILLED with any
specific mandatory requirement flagged by the red warnings — that's the
only adaptive piece.

PERSISTENCE NOTE
----------------
The spec calls for recording the client's answers (audit trail). That needs
the accounts table from the gating PR (chunk 6), which is on a separate
unmerged branch. This module emits the QUESTIONS only; persistence lives
behind the TODO seam below and lands in a follow-up.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from tender_agent.services.go_no_go.warnings import RedWarning

QuestionType = Literal["yes_no"]


@dataclass
class SelfCertificationQuestion:
    id: str
    ask: str
    type: QuestionType
    # When a red warning flagged a specific mandatory requirement, surface it
    # alongside the qualifies question so the client confirms the SPECIFIC
    # item, not a generic "do you qualify".
    prefill: dict[str, Any] | None = None
    related_warnings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "ask": self.ask, "type": self.type}
        if self.prefill is not None:
            out["prefill"] = self.prefill
        if self.related_warnings:
            out["related_warnings"] = list(self.related_warnings)
        return out


def build_self_certification_questions(
    red_warnings: list[RedWarning],
) -> list[SelfCertificationQuestion]:
    """Three questions, in order, with the `qualifies` one pre-filled when
    the red warnings name a specific mandatory requirement."""
    # The mandatory_requirement_unmet warnings carry a `requirement` field
    # (the verbatim ITT line). Show them all so the user confirms every one.
    unmet = [w for w in red_warnings if w.warning == "mandatory_requirement_unmet"]
    qualifies_prefill: dict[str, Any] | None = None
    if unmet:
        qualifies_prefill = {
            "flagged_requirements": [
                {
                    "requirement": w.requirement,
                    "reason": w.detail,
                    "source_document": w.source_document,
                }
                for w in unmet
                if w.requirement
            ],
        }

    return [
        SelfCertificationQuestion(
            id="has_documents",
            ask=(
                "Do you have all the documents and information this tender "
                "requires?"
            ),
            type="yes_no",
        ),
        SelfCertificationQuestion(
            id="qualifies",
            ask=(
                "Do you meet the mandatory requirements to qualify for this "
                "contract?"
            ),
            type="yes_no",
            prefill=qualifies_prefill,
            related_warnings=[w.to_dict() for w in unmet],
        ),
        SelfCertificationQuestion(
            id="accepts_proceed",
            ask=(
                "You've seen our notes and warnings. Proceed to generate "
                "your bid documents?"
            ),
            type="yes_no",
        ),
    ]


# ---------------------------------------------------------------------------
# TODO (post-chunk-6): persist self-certification answers.
#
# When the accounts table from chunk 6 lands on main, add:
#
#   class SelfCertification(Base):
#       __tablename__ = "self_certifications"
#       id, account_id (FK), tender_id (FK), answers JSONB, warnings JSONB,
#       answered_at, recorded_at
#
#   def record_self_certification(db, *, account_id, tender_id, answers,
#                                 warnings_shown) -> SelfCertification:
#       ...
#
# Until then the engine only EMITS the questions; the dashboard collects
# answers client-side and they don't reach the server. That's a deliberate
# seam, not an omission — flagged here so the follow-up is obvious.
# ---------------------------------------------------------------------------
