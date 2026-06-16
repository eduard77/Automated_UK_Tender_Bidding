"""cursor-based backlog resume — drain regardless of feed sort direction

Revision ID: 0020
Revises: 0019
Create Date: 2026-06-15

Adds a pagination-cursor resume point to both `sources` and `poll_runs`:

- sources.resume_cursor: the durable next-page URL the source's backlog drain
  has reached. The next poll resumes its fetch FROM this cursor instead of
  rebuilding an `updatedFrom` window — so the backlog drains forward across
  900s-timeout-cancelled cycles even when the feed is NEWEST-first and the
  date-watermark (PageProgressTracker) correctly refuses to advance.
- poll_runs.resume_cursor: the cursor this run reached, so the sources-health
  diagnosis can see forward progress (a changed cursor run-to-run) on a
  newest-first feed where watermark_at stays NULL.

Why (2026-06-15, 4th pass on the CF/FTS non-advancing loop): #134 made the
date-watermark direction-aware — correct, but on a newest-first feed it
FREEZES (never advances) so a cancelled mid-sweep run kept no resume point and
the next run re-walked from page 1. #134's closing note named the fix: resume
by the OCDS `links.next` cursor, which is available per page regardless of feed
direction. The date-watermark stays as the safe fresh-window floor (`since`);
the cursor is the actual mid-backlog resume point.

Additive + nullable; no backfill (a NULL cursor simply means "start a fresh
window from last_polled_at", the pre-cursor behaviour).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column("resume_cursor", sa.Text(), nullable=True),
    )
    op.add_column(
        "poll_runs",
        sa.Column("resume_cursor", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("poll_runs", "resume_cursor")
    op.drop_column("sources", "resume_cursor")
