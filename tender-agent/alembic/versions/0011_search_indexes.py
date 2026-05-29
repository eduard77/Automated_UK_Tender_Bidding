"""search filter indexes

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-29

Phase 4 chunk 7. Indexes only — the tender model/columns are unchanged. These
back the source-agnostic /tenders/search filters:

  - btree on status and buyer_region (equality / IN filters, ORDER BY)
  - GIN on cpv_codes and keywords (array overlap `&&` and the keyword text scan)
  - pg_trgm GIN on title and description (the free-text `q` ILIKE contains-match)

status, buyer_region, deadline_at and source_code that already exist are left
as-is (deadline_at / source_code were created with the table). Everything here
is created IF NOT EXISTS so the migration is safe on a partially-indexed DB.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Trigram matching for case-insensitive `q` contains-search.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.execute("CREATE INDEX IF NOT EXISTS ix_tenders_status ON tenders (status)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tenders_buyer_region ON tenders (buyer_region)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tenders_cpv_codes_gin "
        "ON tenders USING gin (cpv_codes)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tenders_keywords_gin "
        "ON tenders USING gin (keywords)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tenders_title_trgm "
        "ON tenders USING gin (title gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tenders_description_trgm "
        "ON tenders USING gin (description gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_tenders_description_trgm")
    op.execute("DROP INDEX IF EXISTS ix_tenders_title_trgm")
    op.execute("DROP INDEX IF EXISTS ix_tenders_keywords_gin")
    op.execute("DROP INDEX IF EXISTS ix_tenders_cpv_codes_gin")
    op.execute("DROP INDEX IF EXISTS ix_tenders_buyer_region")
    op.execute("DROP INDEX IF EXISTS ix_tenders_status")
    # Leave the pg_trgm extension installed — other objects may rely on it and
    # dropping an extension is not reversible cleanly.
