"""poll run resume watermark — backlog catch-up convergence + catching_up diagnosis

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-14

Adds poll_runs.watermark_at: the resume point (the adapter's confirmed-page
watermark) that a poll run reached.

Why: the two largest sources (CF, FTS) carry ~12 days of backlog that a full
catch-up sweep can't drain inside the 900 s per-source poll timeout. The
timeout cancels the run (correct self-heal), but progress used to be discarded
on cancellation, so the next run restarted from the backlog start and timed
out again — an infinite non-advancing loop. poll_source now persists the
resume watermark INCREMENTALLY as pages confirm (so a cancellation keeps it),
and stamps it on the run here. The sources-health diagnosis compares this
across consecutive runs to tell a backlog drain (catching_up) from a stuck
source (fetch_failing).

Additive + nullable; no backfill (older runs simply read NULL).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "poll_runs",
        sa.Column("watermark_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("poll_runs", "watermark_at")
