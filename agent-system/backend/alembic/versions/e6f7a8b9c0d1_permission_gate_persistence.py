"""Permission gate persistence + cost-accounting scopes.

Revision ID: e6f7a8b9c0d1
Revises: c3d4e5f6a7b8
Create Date: 2026-09-16

The permission gate was split in two: an in-memory ``PermissionGate`` serving
``/api/v1/approvals`` and a separate set of DB helpers inside the tool layer.
An approval granted through the API therefore could never unblock a tool.

This migration makes the `approvals` table the single durable decision store
so one authoritative gate can serve both paths, and records the grant policy
(ALLOW_ONCE / ALLOW_SESSION / ALLOW_WORKSPACE / ALLOW_ALWAYS / DENY) plus the
scope identifiers the policy needs to be evaluated:

  - ``policy``      the grant policy attached when the request was decided
  - ``consumed``    ALLOW_ONCE grants are consumed on first successful check
  - ``session_id``  ALLOW_SESSION scoping
  - ``workspace_id`` ALLOW_WORKSPACE scoping
  - ``context_json`` request context (already carried by the in-memory record)

It also adds ``model_calls.session_id`` so budget accounting can be derived
from persisted records per daily / session / task / provider scope instead of
process-local counters, and indexes the columns those aggregates filter on.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e6f7a8b9c0d1"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("approvals", schema=None) as batch_op:
        batch_op.add_column(sa.Column("session_id", sa.String(40), nullable=True))
        batch_op.add_column(sa.Column("workspace_id", sa.String(40), nullable=True))
        batch_op.add_column(
            sa.Column("context_json", sa.JSON(), nullable=False, server_default="{}")
        )
        batch_op.add_column(
            sa.Column("policy", sa.String(20), nullable=False, server_default="ALLOW_ONCE")
        )
        batch_op.add_column(
            sa.Column("consumed", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.create_index(batch_op.f("ix_approvals_scope"), ["scope"], unique=False)
        batch_op.create_index(batch_op.f("ix_approvals_session_id"), ["session_id"], unique=False)

    with op.batch_alter_table("model_calls", schema=None) as batch_op:
        batch_op.add_column(sa.Column("session_id", sa.String(40), nullable=True))
        batch_op.create_index(batch_op.f("ix_model_calls_session_id"), ["session_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_model_calls_provider"), ["provider"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_model_calls_created_at"), ["created_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("model_calls", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_model_calls_created_at"))
        batch_op.drop_index(batch_op.f("ix_model_calls_provider"))
        batch_op.drop_index(batch_op.f("ix_model_calls_session_id"))
        batch_op.drop_column("session_id")

    with op.batch_alter_table("approvals", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_approvals_session_id"))
        batch_op.drop_index(batch_op.f("ix_approvals_scope"))
        batch_op.drop_column("consumed")
        batch_op.drop_column("policy")
        batch_op.drop_column("context_json")
        batch_op.drop_column("workspace_id")
        batch_op.drop_column("session_id")
