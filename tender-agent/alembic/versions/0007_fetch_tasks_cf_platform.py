"""fetch_tasks table + contracts_finder_direct platform

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-20

Phase 4 chunk 3. Adds:
- `fetch_tasks` — durable record of each 'fetch documents' run (replaces the
  chunk-2 in-memory stub). One row per POST /tenders/{id}/fetch-documents.
- the `contracts_finder_direct` platform: the first platform with a real
  (read_only) adapter. Its domain_patterns match assets.publishing.service.gov.uk
  — the public gov asset host — and its adapter downloads those files with
  plain httpx (no auth, no browser).

Fetched documents reuse the existing `tender_document_files` table (created in
migration 0002); no separate documents table is introduced.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CF_PLATFORM = {
    "slug": "contracts_finder_direct",
    "vendor": "Crown Commercial Service",
    "display_name": "Contracts Finder (direct)",
    "kind": "special",
    "login_type": "none",
    "adapter_status": "read_only",
    "adapter_module": (
        "tender_agent.services.portals.contracts_finder."
        "ContractsFinderDirectAdapter"
    ),
    "domain_patterns": [r"(^|\.)assets\.publishing\.service\.gov\.uk$"],
    "notes": (
        "Public gov asset host. Documents are fetched over plain HTTP, no "
        "auth, no browser. Buyer-portal URLs are rejected by the adapter."
    ),
}


def upgrade() -> None:
    op.create_table(
        "fetch_tasks",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column(
            "tender_id",
            sa.BigInteger(),
            sa.ForeignKey("tenders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("adapter", sa.Text()),
        sa.Column("platform_slug", sa.Text()),
        sa.Column("files_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("missing_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("detail", sa.Text()),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text())),
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
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_fetch_tasks_task_id", "fetch_tasks", ["task_id"], unique=True)
    op.create_index("ix_fetch_tasks_tender_id", "fetch_tasks", ["tender_id"])

    platforms = sa.table(
        "portal_platforms",
        sa.column("slug", sa.Text()),
        sa.column("vendor", sa.Text()),
        sa.column("display_name", sa.Text()),
        sa.column("domain_patterns", postgresql.JSONB()),
        sa.column("kind", sa.Text()),
        sa.column("login_type", sa.Text()),
        sa.column("adapter_status", sa.Text()),
        sa.column("adapter_module", sa.Text()),
        sa.column("notes", sa.Text()),
    )
    op.bulk_insert(
        platforms,
        [
            {
                "slug": CF_PLATFORM["slug"],
                "vendor": CF_PLATFORM["vendor"],
                "display_name": CF_PLATFORM["display_name"],
                "domain_patterns": CF_PLATFORM["domain_patterns"],
                "kind": CF_PLATFORM["kind"],
                "login_type": CF_PLATFORM["login_type"],
                "adapter_status": CF_PLATFORM["adapter_status"],
                "adapter_module": CF_PLATFORM["adapter_module"],
                "notes": CF_PLATFORM["notes"],
            }
        ],
    )

    # Link any already-discovered assets.publishing.service.gov.uk portal.
    bind = op.get_bind()
    row = bind.execute(
        sa.text("SELECT id FROM portal_platforms WHERE slug = :s"),
        {"s": CF_PLATFORM["slug"]},
    ).fetchone()
    if row is not None:
        platform_id = row[0]
        bind.execute(
            sa.text(
                "UPDATE portals SET platform_id = :pid "
                "WHERE domain ~ '(^|\\.)assets\\.publishing\\.service\\.gov\\.uk$' "
                "AND platform_id IS NULL"
            ),
            {"pid": platform_id},
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM portal_platforms WHERE slug = 'contracts_finder_direct'"
    )
    op.drop_index("ix_fetch_tasks_tender_id", table_name="fetch_tasks")
    op.drop_index("ix_fetch_tasks_task_id", table_name="fetch_tasks")
    op.drop_table("fetch_tasks")
