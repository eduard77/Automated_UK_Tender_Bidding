"""submission_packages + submission_question_drafts

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-01

Phase 4 submission-package generator (drafting spine). Adds two tables that
store the output of the drafting agent for human review:

* submission_packages — one row per tender, groups question drafts.
* submission_question_drafts — one row per drafted question (template +
  structured content + citations + validation report).

There is intentionally no 'submitted' state on the draft status enum — the
spec is explicit that submission is human-gated, lives on the portal rail,
and is never something the drafting agent can flip on its own.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "submission_packages",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tender_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "org_id",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default=sa.text("'drafting'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["tender_id"], ["tenders.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_submission_packages_tender_id",
        "submission_packages",
        ["tender_id"],
    )
    op.create_index(
        "ix_submission_packages_org_id", "submission_packages", ["org_id"]
    )

    op.create_table(
        "submission_question_drafts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("package_id", sa.BigInteger(), nullable=False),
        sa.Column("tender_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "org_id",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("template_id", sa.String(length=48), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("question_ref", sa.Text(), nullable=False),
        sa.Column(
            "structured_content", postgresql.JSONB(astext_type=sa.Text())
        ),
        sa.Column(
            "vault_citations", postgresql.JSONB(astext_type=sa.Text())
        ),
        sa.Column(
            "confidence_scores", postgresql.JSONB(astext_type=sa.Text())
        ),
        sa.Column(
            "unfilled_slots", postgresql.JSONB(astext_type=sa.Text())
        ),
        sa.Column(
            "validation_report", postgresql.JSONB(astext_type=sa.Text())
        ),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default=sa.text("'needs_review'"),
        ),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["package_id"], ["submission_packages.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["tender_id"], ["tenders.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_submission_question_drafts_package_id",
        "submission_question_drafts",
        ["package_id"],
    )
    op.create_index(
        "ix_submission_question_drafts_tender_id",
        "submission_question_drafts",
        ["tender_id"],
    )
    op.create_index(
        "ix_submission_question_drafts_org_id",
        "submission_question_drafts",
        ["org_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_submission_question_drafts_org_id",
        table_name="submission_question_drafts",
    )
    op.drop_index(
        "ix_submission_question_drafts_tender_id",
        table_name="submission_question_drafts",
    )
    op.drop_index(
        "ix_submission_question_drafts_package_id",
        table_name="submission_question_drafts",
    )
    op.drop_table("submission_question_drafts")
    op.drop_index(
        "ix_submission_packages_org_id", table_name="submission_packages"
    )
    op.drop_index(
        "ix_submission_packages_tender_id", table_name="submission_packages"
    )
    op.drop_table("submission_packages")
