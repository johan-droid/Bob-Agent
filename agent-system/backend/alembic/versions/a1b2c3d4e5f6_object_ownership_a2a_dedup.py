"""Object ownership + A2A replay dedup.

Revision ID: a1b2c3d4e5f6
Revises: f9b0c1d2e3f4
Create Date: 2026-09-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "f9b0c1d2e3f4"
branch_labels = None
depends_on = None

_OWNER_TABLES = [
    "workspaces",
    "artifacts",
    "workspace_templates",
    "behavior_recordings",
    "task_batches",
    "qa_reports",
    "recipes",
    "insights",
    "scheduled_jobs",
    "model_calls",
    "tool_calls",
]


def upgrade() -> None:
    for table in _OWNER_TABLES:
        try:
            op.add_column(table, sa.Column("owner_user_id", sa.String(length=40), nullable=True))
        except Exception:
            pass
        try:
            op.create_index(op.f(f"ix_{table}_owner_user_id"), table, ["owner_user_id"], unique=False)
        except Exception:
            pass
    op.create_table(
        "a2a_processed_envelopes",
        sa.Column("envelope_hash", sa.String(length=64), nullable=False),
        sa.Column("delegation_id", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("envelope_hash", name=op.f("pk_a2a_processed_envelopes")),
    )


def downgrade() -> None:
    op.drop_table("a2a_processed_envelopes")
    for table in _OWNER_TABLES:
        try:
            op.drop_index(op.f(f"ix_{table}_owner_user_id"), table_name=table)
        except Exception:
            pass
        try:
            op.drop_column(table, "owner_user_id")
        except Exception:
            pass
