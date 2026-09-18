"""Agentic Runtime v1: swarm membership table (additive).

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6

Additive only — no existing table is altered.

``swarm_members`` records that a worker task belongs to one durable master
task. Workers are ordinary tasks (same lifecycle, permissions, verifier);
this table only tracks role, membership and verification state.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c2d3e4f5a6b7"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "swarm_members",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("master_task_id", sa.String(length=40), nullable=False),
        sa.Column("worker_task_id", sa.String(length=40), nullable=False),
        sa.Column("role", sa.String(length=40), nullable=False, server_default="CODER"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["master_task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["worker_task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_swarm_members")),
        sa.UniqueConstraint("worker_task_id", name="uq_swarm_members_worker_task_id"),
    )
    op.create_index(
        op.f("ix_swarm_members_master_task_id"),
        "swarm_members",
        ["master_task_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_swarm_members_worker_task_id"),
        "swarm_members",
        ["worker_task_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_swarm_members_worker_task_id"), table_name="swarm_members"
    )
    op.drop_index(
        op.f("ix_swarm_members_master_task_id"), table_name="swarm_members"
    )
    op.drop_table("swarm_members")
