"""region column + region_lookup cache

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-29

Phase 4 chunk 8. Adds the canonical ``region`` column to ``tenders`` (extracted
from raw OCDS delivery-address data; ``buyer_region`` is left as-is as the raw
source value) plus a btree index for the search region filter, and the
``region_lookup`` cache table so each distinct unresolved location key is
resolved once and reused.

The column add / index use IF [NOT] EXISTS so re-applying against a
partially-migrated DB is safe.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE tenders ADD COLUMN IF NOT EXISTS region VARCHAR(64)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_tenders_region ON tenders (region)")

    op.create_table(
        "region_lookup",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("raw_key", sa.Text(), nullable=False),
        sa.Column("canonical_region", sa.String(length=64), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("raw_key", name="uq_region_lookup_raw_key"),
    )


def downgrade() -> None:
    op.drop_table("region_lookup")
    op.execute("DROP INDEX IF EXISTS ix_tenders_region")
    op.execute("ALTER TABLE tenders DROP COLUMN IF EXISTS region")
