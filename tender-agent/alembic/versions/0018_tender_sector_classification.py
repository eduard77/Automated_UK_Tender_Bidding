"""tender sector classification — the normalisation spine (Phase 3a)

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-12

Adds AI-assigned sector classification to every tender:
- primary_sector   : exactly one top-level sector (indexed for filtering)
- secondary_sectors: JSON list of additional applicable sectors
- subcategories    : JSON dict {sector: [sub-categories]} — a dict (not a
                     construction-only column) so other sectors' sub-lists can
                     be added later with NO further migration; only Construction
                     is populated today
- classified_at    : when it was last classified
- classifier_version: stamps the taxonomy/prompt version so a bump re-runs only
                     stale rows (indexed — the worker/backfill scan on it)

No backfill in the migration itself; the engine classifies new tenders via the
enrichment worker and the existing pool via POST /admin/classify/backfill.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenders", sa.Column("primary_sector", sa.String(length=64), nullable=True)
    )
    op.add_column("tenders", sa.Column("secondary_sectors", sa.JSON(), nullable=True))
    op.add_column("tenders", sa.Column("subcategories", sa.JSON(), nullable=True))
    op.add_column(
        "tenders",
        sa.Column("classified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tenders",
        sa.Column("classifier_version", sa.String(length=32), nullable=True),
    )
    op.create_index("ix_tenders_primary_sector", "tenders", ["primary_sector"])
    op.create_index(
        "ix_tenders_classifier_version", "tenders", ["classifier_version"]
    )


def downgrade() -> None:
    op.drop_index("ix_tenders_classifier_version", table_name="tenders")
    op.drop_index("ix_tenders_primary_sector", table_name="tenders")
    op.drop_column("tenders", "classifier_version")
    op.drop_column("tenders", "classified_at")
    op.drop_column("tenders", "subcategories")
    op.drop_column("tenders", "secondary_sectors")
    op.drop_column("tenders", "primary_sector")
