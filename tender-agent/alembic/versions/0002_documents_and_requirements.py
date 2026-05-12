"""add document files and requirements tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-07
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tender_document_files",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "tender_id",
            sa.BigInteger(),
            sa.ForeignKey("tenders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("title", sa.String(length=512)),
        sa.Column("format", sa.String(length=32)),
        sa.Column("storage_key", sa.String(length=512)),
        sa.Column("storage_backend", sa.String(length=16), nullable=False, server_default="local"),
        sa.Column("bytes", sa.BigInteger()),
        sa.Column("sha256", sa.String(length=64)),
        sa.Column("text_extracted", sa.Text()),
        sa.Column(
            "download_status", sa.String(length=16), nullable=False, server_default="pending"
        ),
        sa.Column("error", sa.Text()),
        sa.Column("downloaded_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("tender_id", "url", name="uq_doc_tender_url"),
    )
    op.create_index("ix_tender_document_files_tender_id", "tender_document_files", ["tender_id"])

    op.create_table(
        "tender_requirements",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "tender_id",
            sa.BigInteger(),
            sa.ForeignKey("tenders.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("summary", sa.Text()),
        sa.Column("evaluation_criteria", postgresql.JSONB()),
        sa.Column("mandatory_requirements", postgresql.JSONB()),
        sa.Column("desired_requirements", postgresql.JSONB()),
        sa.Column("documents_required", postgresql.JSONB()),
        sa.Column("questions_to_answer", postgresql.JSONB()),
        sa.Column("risk_flags", postgresql.ARRAY(sa.String())),
        sa.Column("estimated_effort_days", sa.Numeric(5, 2)),
        sa.Column("recommendation", sa.String(length=32)),
        sa.Column("recommendation_reason", sa.Text()),
        sa.Column("model", sa.String(length=64)),
        sa.Column(
            "extracted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("raw_response", postgresql.JSONB()),
    )
    op.create_index("ix_tender_requirements_tender_id", "tender_requirements", ["tender_id"])


def downgrade() -> None:
    op.drop_table("tender_requirements")
    op.drop_table("tender_document_files")
