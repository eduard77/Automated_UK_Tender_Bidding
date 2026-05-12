"""add vault tables + pgvector extension

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-12
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


VAULT_CATEGORIES = (
    "corporate",
    "financial",
    "insurance",
    "accreditation",
    "policy",
    "capability",
    "people",
    "technical",
    "past_bid",
    "uncategorised",
)
CONFIDENTIALITY_LEVELS = ("public", "internal", "confidential")


def upgrade() -> None:
    # Postgres extension for pgvector. Some managed instances require the role
    # to be granted rds_superuser (or equivalent) to install extensions; the
    # deploy runbook handles that.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "vault_documents",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("org_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("subcategory", sa.String(length=64)),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("owner_email", sa.String(length=256)),
        sa.Column(
            "confidentiality",
            sa.String(length=16),
            nullable=False,
            server_default="internal",
        ),
        sa.Column("current_version_id", sa.BigInteger()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "category IN ({})".format(
                ", ".join(f"'{c}'" for c in VAULT_CATEGORIES)
            ),
            name="ck_vault_documents_category",
        ),
        sa.CheckConstraint(
            "confidentiality IN ({})".format(
                ", ".join(f"'{c}'" for c in CONFIDENTIALITY_LEVELS)
            ),
            name="ck_vault_documents_confidentiality",
        ),
    )
    op.create_index("ix_vault_documents_org_id", "vault_documents", ["org_id"])
    op.create_index(
        "ix_vault_documents_category", "vault_documents", ["category"]
    )

    op.create_table(
        "vault_document_versions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "document_id",
            sa.BigInteger(),
            sa.ForeignKey("vault_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(length=1024), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("expiry_date", sa.Date()),
        sa.Column("issuing_body", sa.String(length=256)),
        sa.Column("issued_date", sa.Date()),
        sa.Column("last_reviewed_date", sa.Date()),
        sa.Column(
            "superseded_by_version_id",
            sa.BigInteger(),
            sa.ForeignKey("vault_document_versions.id"),
        ),
        sa.Column(
            "claims",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "claims_confirmed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("text_extracted", sa.Text()),
        sa.Column("embedding", Vector(1536)),
        sa.Column("uploaded_by", sa.String(length=256)),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("document_id", "version", name="uq_vault_version"),
    )
    op.create_index(
        "ix_vault_versions_document_id",
        "vault_document_versions",
        ["document_id"],
    )
    op.create_index(
        "ix_vault_versions_sha256", "vault_document_versions", ["sha256"]
    )
    op.create_index(
        "ix_vault_versions_expiry", "vault_document_versions", ["expiry_date"]
    )
    op.create_index(
        "ix_vault_versions_superseded_by",
        "vault_document_versions",
        ["superseded_by_version_id"],
    )

    # IVFFlat index on the embedding for fast approximate nearest-neighbour.
    # `lists` tuned for a few thousand documents; revisit if the vault grows
    # past ~50k versions (rule of thumb: lists = sqrt(rowcount)).
    op.execute(
        "CREATE INDEX ix_vault_versions_embedding "
        "ON vault_document_versions USING ivfflat (embedding vector_cosine_ops) "
        "WITH (lists = 100)"
    )

    # The vault_documents.current_version_id FK needs to be added AFTER
    # vault_document_versions exists. SET NULL on delete + use_alter handles
    # the chicken-and-egg.
    op.create_foreign_key(
        "fk_vault_documents_current_version",
        "vault_documents",
        "vault_document_versions",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_vault_documents_current_version",
        "vault_documents",
        type_="foreignkey",
    )
    op.execute("DROP INDEX IF EXISTS ix_vault_versions_embedding")
    op.drop_index(
        "ix_vault_versions_superseded_by", table_name="vault_document_versions"
    )
    op.drop_index(
        "ix_vault_versions_expiry", table_name="vault_document_versions"
    )
    op.drop_index(
        "ix_vault_versions_sha256", table_name="vault_document_versions"
    )
    op.drop_index(
        "ix_vault_versions_document_id", table_name="vault_document_versions"
    )
    op.drop_table("vault_document_versions")
    op.drop_index("ix_vault_documents_category", table_name="vault_documents")
    op.drop_index("ix_vault_documents_org_id", table_name="vault_documents")
    op.drop_table("vault_documents")
    # We intentionally do NOT drop the vector extension on downgrade — other
    # Phase-3+ code (e.g. case-study narrative similarity) will use it too.
