"""Ollama Cloud-first inference runtime: session model locks + task checkpoints.

Revision ID: c4d5e6f7a8b9
Revises: b2c3d4e5f6a7
Create Date: 2026-09-24

Additive only:

- ``inference_model_locks``: one durable row per session recording the
  selected provider/model. The in-process lock governs a live run; this row
  makes the lock survive a restart and stay observable.
- ``inference_checkpoints``: one idempotent row per task written before an
  emergency fallback, holding only what is needed to resume (task state,
  context reference, completed/pending steps, tool results, active
  provider/model). Secrets are redacted before storage.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c4d5e6f7a8b9"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inference_model_locks",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("session_id", sa.String(length=40), nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=True),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("model_id", sa.String(length=120), nullable=False),
        sa.Column("role", sa.String(length=24), nullable=False, server_default="GENERAL"),
        sa.Column("task_family", sa.String(length=24), nullable=False, server_default="CHAT"),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_inference_model_locks_session_id",
        "inference_model_locks",
        ["session_id"],
        unique=True,
    )
    op.create_index(
        "ix_inference_model_locks_task_id", "inference_model_locks", ["task_id"]
    )
    op.create_index(
        "ix_inference_model_locks_updated_at", "inference_model_locks", ["updated_at"]
    )

    op.create_table(
        "inference_checkpoints",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("session_id", sa.String(length=40), nullable=True),
        sa.Column("step_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="open"),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("model_id", sa.String(length=120), nullable=False),
        sa.Column("error_kind", sa.String(length=32), nullable=False, server_default="UNKNOWN"),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resumed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_inference_checkpoints_task_id", "inference_checkpoints", ["task_id"], unique=True
    )
    op.create_index(
        "ix_inference_checkpoints_session_id", "inference_checkpoints", ["session_id"]
    )
    op.create_index(
        "ix_inference_checkpoints_updated_at", "inference_checkpoints", ["updated_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_inference_checkpoints_updated_at", table_name="inference_checkpoints")
    op.drop_index("ix_inference_checkpoints_session_id", table_name="inference_checkpoints")
    op.drop_index("ix_inference_checkpoints_task_id", table_name="inference_checkpoints")
    op.drop_table("inference_checkpoints")
    op.drop_index("ix_inference_model_locks_updated_at", table_name="inference_model_locks")
    op.drop_index("ix_inference_model_locks_task_id", table_name="inference_model_locks")
    op.drop_index("ix_inference_model_locks_session_id", table_name="inference_model_locks")
    op.drop_table("inference_model_locks")
