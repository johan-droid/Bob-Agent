"""Cloud vault: durable memory notes table (Postgres/SQLite).

Revision ID: c3d4e5f6a7b8
Revises: 7c1a2d9e4f50
Create Date: 2026-09-12

Local dev keeps Obsidian markdown notes; dyno filesystems are ephemeral,
so cloud deployments persist the same scrubbed content in `memory_notes`.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c3d4e5f6a7b8"
down_revision = "7c1a2d9e4f50"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_notes",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("layer", sa.String(20), nullable=False, server_default="TASK"),
        sa.Column("source", sa.String(40), nullable=False, server_default="agent"),
        sa.Column("tags_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("links_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("session_id", sa.String(40), nullable=True),
        sa.Column("task_id", sa.String(40), nullable=True),
        sa.Column("agent_run_id", sa.String(40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_memory_notes_layer", "memory_notes", ["layer"])
    op.create_index("ix_memory_notes_session_id", "memory_notes", ["session_id"])
    op.create_index("ix_memory_notes_task_id", "memory_notes", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_memory_notes_task_id", table_name="memory_notes")
    op.drop_index("ix_memory_notes_session_id", table_name="memory_notes")
    op.drop_index("ix_memory_notes_layer", table_name="memory_notes")
    op.drop_table("memory_notes")
