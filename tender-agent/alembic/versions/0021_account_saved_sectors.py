"""per-user saved sector preferences — Phase 3b setup-page persistence

Revision ID: 0021
Revises: 0020
Create Date: 2026-06-17

Adds accounts.saved_sectors: the list of taxonomy sectors a user picked on the
setup/preferences page. Pre-applied as the default sector filter when that user
returns to the dashboard (they can still override ad-hoc without changing the
saved set).

Per-user by construction — the column lives on the account row, so one user's
saved sectors can never bleed into another's (same isolation model as the
per-user push subscriptions and portal connections). Stored as a JSON list of
canonical sector strings; only values from the fixed taxonomy are ever written.

Additive + nullable; no backfill (a NULL column simply means "no saved sectors
yet" — the dashboard shows everything until the user saves a selection).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("saved_sectors", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("accounts", "saved_sectors")
