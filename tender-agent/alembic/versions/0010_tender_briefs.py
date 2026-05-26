"""tender_briefs table

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-26

Phase 4 chunk 5 part C. Each generate-brief request creates a row here;
regenerating adds another row, keeping history. The dashboard reads the
latest row by tender_id.

Status values (app-level, no DB enum):
  generating, complete, failed
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tender_briefs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "tender_id",
            sa.BigInteger(),
            sa.ForeignKey("tenders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="generating"),
        sa.Column("recommendation", sa.Text()),
        sa.Column("confidence", sa.Text()),
        sa.Column("headline", sa.Text()),
        sa.Column("brief_json", postgresql.JSONB()),
        sa.Column("model", sa.Text()),
        sa.Column("documents_considered", postgresql.JSONB()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("error_detail", sa.Text()),
        sa.Column("generated_at", sa.DateTime(timezone=True)),
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
    )
    op.create_index(
        "ix_tender_briefs_tender_created",
        "tender_briefs",
        ["tender_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_tender_briefs_tender_created", table_name="tender_briefs")
    op.drop_table("tender_briefs")
