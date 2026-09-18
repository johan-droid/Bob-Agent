"""Agentic Runtime v1: durable worker attempts ledger (additive).

Revision ID: b1c2d3e4f5a6
Revises: f8a9b0c1d2e3

Additive only — no existing table is altered.

``worker_attempts`` is the fallback ledger: one row per provider attempt
under the SAME task_id/worker_id. Fallback changes only provider/model/
attempt_id; task_id, worker context, tool state and memory stay put so
tool calls survive provider switching.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b1c2d3e4f5a6"
down_revision = "f8a9b0c1d2e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_attempts",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("worker_id", sa.String(length=40), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("model_id", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="started"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("tool_calls_made", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_worker_attempts")),
    )
    op.create_index(
        op.f("ix_worker_attempts_task_id"), "worker_attempts", ["task_id"], unique=False
    )
    op.create_index(
        op.f("ix_worker_attempts_worker_id"), "worker_attempts", ["worker_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_worker_attempts_worker_id"), table_name="worker_attempts")
    op.drop_index(op.f("ix_worker_attempts_task_id"), table_name="worker_attempts")
    op.drop_table("worker_attempts")
