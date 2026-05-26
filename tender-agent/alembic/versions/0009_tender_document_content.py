"""tender_document_content store

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-26

Phase 4 chunk 5 part A. Adds the per-document extracted-content store: each
unique (sha256, extractor_version) pair is extracted ONCE and reused across
every tender it appears on. The DB is now the durable home for document text
— files on disk become a cache for the bytes, content becomes first-class
data we can move to a cloud DB and use for cross-tender comparison.

The UNIQUE (sha256, extractor_version) constraint is what lets a re-fetch
(or another tender that pulls the same ITT) skip extraction entirely.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

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
        sa.UniqueConstraint(
            "sha256", "extractor_version", name="uq_tdc_sha_version"
        ),
    )
    op.create_index(
        "ix_tender_document_content_tender_id",
        "tender_document_content",
        ["tender_id"],
    )
    op.create_index(
        "ix_tender_document_content_document_file_id",
        "tender_document_content",
        ["document_file_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tender_document_content_document_file_id",
        table_name="tender_document_content",
    )
    op.drop_index(
        "ix_tender_document_content_tender_id",
        table_name="tender_document_content",
    )
    op.drop_table("tender_document_content")
