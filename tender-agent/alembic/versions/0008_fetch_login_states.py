"""fetch_tasks login/confirm columns

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-20

Phase 4 chunk 4. Adds the columns the login-via-human flow needs to fetch_tasks.

fetch_tasks.status is free text (no DB enum/constraint), so the new status
values introduced by this chunk are app-level only:
    queued, running, waiting_for_login, needs_user_confirmation, complete,
    partial, nothing_available, no_portal, failed, error
This migration only adds the supporting columns.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("fetch_tasks", sa.Column("login_url", sa.Text(), nullable=True))
    op.add_column(
        "fetch_tasks", sa.Column("bridge_session_slug", sa.Text(), nullable=True)
    )
    op.add_column(
        "fetch_tasks",
        sa.Column("waiting_since", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("fetch_tasks", "waiting_since")
    op.drop_column("fetch_tasks", "bridge_session_slug")
    op.drop_column("fetch_tasks", "login_url")
