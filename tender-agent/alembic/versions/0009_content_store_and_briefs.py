"""tender_document_content store + tender_briefs

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-26

Phase 4 chunk 5. Two additions:

- `tender_document_content` — the durable, reusable store for the *extracted
  text* of every document we've fetched. Content is keyed by sha256 + extractor
  version, so the same document (same bytes) is extracted ONCE and reused
  forever across tenders — re-running fetch on a tender we've already pulled
  must not re-extract, and a second tender containing the same ITT file must
  reuse the existing extraction rather than redo it. The actual files keep
  living under DOCUMENT_STORAGE_DIR (or wherever the file backend points) —
  this migration is purely about content as data.

- `tender_briefs` — one row per generated bid-brief (history retained; the
  latest row per tender wins in the UI). brief_json holds the validated JSON
  payload from Claude; the headline fields are mirrored as columns for cheap
  list / filter queries.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tender_document_content",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "document_file_id",
            sa.BigInteger(),
            sa.ForeignKey("tender_document_files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tender_id",
            sa.BigInteger(),
            sa.ForeignKey("tenders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("extracted_text", sa.Text()),
        sa.Column("char_count", sa.Integer()),
        sa.Column("extraction_status", sa.Text(), nullable=False),
        sa.Column("extraction_detail", sa.Text()),
        sa.Column("doc_type", sa.Text()),
        sa.Column("extractor_version", sa.Text(), nullable=False),
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
        # Same content bytes + same extractor version = one canonical extraction.
        # Reusable across tenders by sha256 lookup.
        sa.UniqueConstraint(
            "sha256", "extractor_version", name="uq_content_sha_extractor"
        ),
    )
    op.create_index(
        "ix_tender_document_content_tender_id",
        "tender_document_content",
        ["tender_id"],
    )
    op.create_index(
        "ix_tender_document_content_doc_file_id",
        "tender_document_content",
        ["document_file_id"],
    )
    op.create_index(
        "ix_tender_document_content_sha256",
        "tender_document_content",
        ["sha256"],
    )

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
        sa.Column("brief_json", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("model", sa.Text()),
        sa.Column("documents_considered", postgresql.JSONB(astext_type=sa.Text())),
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
    op.drop_index(
        "ix_tender_document_content_sha256", table_name="tender_document_content"
    )
    op.drop_index(
        "ix_tender_document_content_doc_file_id",
        table_name="tender_document_content",
    )
    op.drop_index(
        "ix_tender_document_content_tender_id",
        table_name="tender_document_content",
    )
    op.drop_table("tender_document_content")
