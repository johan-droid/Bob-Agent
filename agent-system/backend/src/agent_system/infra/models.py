"""ORM models for the canonical domain (v3.1 §5 + v3.0 §7 tables).

JSON columns hold structured payloads validated by Pydantic at service
boundaries. All IDs are prefixed ULID strings (see domain.ids).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from agent_system.domain.events import utcnow


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[str]:
    return mapped_column(String(40), primary_key=True)


def _ts(index: bool = False) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=index)


# ---------------------------------------------------------------------------
# Core orchestration
# ---------------------------------------------------------------------------


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = _pk()
    owner_user_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", nullable=False)
    created_at: Mapped[datetime] = _ts()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = _pk()
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_type: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    depends_on_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    state: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False, index=True)
    agent_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    batch_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_tasks_idempotency_key"),)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = _pk()
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_type: Mapped[str] = mapped_column(String(40), nullable=False)
    state: Mapped[str] = mapped_column(String(24), default="CREATED", nullable=False, index=True)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = _ts()
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class AgentLease(Base):
    """Lease/heartbeat tracking — orphan detection (v3.1 §8)."""

    __tablename__ = "agent_leases"

    agent_run_id: Mapped[str] = _pk()
    worker_id: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    heartbeat_at: Mapped[datetime] = _ts()
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Approval(Base):
    """The single durable permission-decision store (v3.1 §12–§13).

    Both the tool execution path and the ``/api/v1/approvals`` endpoints read
    and write these rows through one :class:`~agent_system.services.permissions.PermissionGate`,
    so an approval granted by a user is always visible to the capability that
    requested it — including across a process restart. The ``policy`` column
    records the grant breadth (ALLOW_ONCE consumes on first use) and the scope
    identifiers let ALLOW_SESSION / ALLOW_WORKSPACE be evaluated.
    """

    __tablename__ = "approvals"

    id: Mapped[str] = _pk()
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    workspace_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    requested_action: Mapped[str] = mapped_column(Text, nullable=False)
    risk: Mapped[str] = mapped_column(String(12), nullable=False)  # LOW/MEDIUM/HIGH/CRITICAL
    scope: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    requester: Mapped[str] = mapped_column(String(60), nullable=False)
    decision: Mapped[str] = mapped_column(String(12), default="PENDING", nullable=False, index=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    policy: Mapped[str] = mapped_column(String(20), default="ALLOW_ONCE", nullable=False)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    context_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(60), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _ts()
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = _pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    container_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="CREATED", nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    template_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    cloned_from_template: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    owner_user_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    created_at: Mapped[datetime] = _ts()
    last_modified_at: Mapped[datetime] = _ts()


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = _pk()
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # pptx/pdf/docx/xlsx/file
    path: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    owner_user_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    created_at: Mapped[datetime] = _ts()


# ---------------------------------------------------------------------------
# v3.1 additions
# ---------------------------------------------------------------------------


class EventRow(Base):
    """Append-only canonical event store (v3.1 §6)."""

    __tablename__ = "events"

    event_id: Mapped[str] = _pk()
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp: Mapped[datetime] = _ts()
    type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(60), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    visibility: Mapped[str] = mapped_column(String(12), default="user", nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(12), default="normal", nullable=False)

    __table_args__ = (
        Index("ix_events_session_sequence", "session_id", "sequence"),
        UniqueConstraint("sequence", name="uq_events_sequence"),
    )


class ModelCall(Base):
    """One model invocation — also the authoritative cost record (v3.1 §20).

    Budget checks aggregate these rows instead of process-local counters, so
    spend survives a restart. ``session_id`` scopes session budgets; task and
    provider scopes use their own columns.
    """

    __tablename__ = "model_calls"

    id: Mapped[str] = _pk()
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    model_id: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # ok/failed
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_cached: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage_is_estimated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_is_estimated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    created_at: Mapped[datetime] = _ts(index=True)


class ToolCall(Base):
    __tablename__ = "tool_calls"

    id: Mapped[str] = _pk()
    agent_run_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    tool_name: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # ok/failed
    risk: Mapped[str] = mapped_column(String(12), default="LOW", nullable=False)
    approval_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    started_at: Mapped[datetime] = _ts()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    key: Mapped[str] = _pk()
    operation: Mapped[str] = mapped_column(String(60), nullable=False)
    result_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = _ts()


# ---------------------------------------------------------------------------
# v3.0 feature tables
# ---------------------------------------------------------------------------


class WorkspaceTemplate(Base):
    __tablename__ = "workspace_templates"

    id: Mapped[str] = _pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source_workspace_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    snapshot_path: Mapped[str] = mapped_column(Text, nullable=False)
    git_history_json: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    tags_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    used_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    owner_user_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    created_at: Mapped[datetime] = _ts()


class BehaviorRecording(Base):
    __tablename__ = "behavior_recordings"

    id: Mapped[str] = _pk()
    session_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    recording_start: Mapped[datetime] = _ts()
    recording_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    action_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    action_log_path: Mapped[str] = mapped_column(Text, nullable=False)
    owner_user_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)


class CostBudget(Base):
    __tablename__ = "cost_budget"

    id: Mapped[str] = _pk()
    period: Mapped[str] = mapped_column(String(12), nullable=False)  # daily/weekly/monthly
    scope: Mapped[str] = mapped_column(String(40), default="global", nullable=False)
    limit_usd: Mapped[float] = mapped_column(Float, nullable=False)
    current_period_start: Mapped[datetime] = _ts()
    alert_threshold_pct: Mapped[int] = mapped_column(Integer, default=80, nullable=False)


class TaskBatch(Base):
    __tablename__ = "task_batches"

    id: Mapped[str] = _pk()
    batch_type: Mapped[str] = mapped_column(String(40), nullable=False)
    task_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    member_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    owner_user_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    created_at: Mapped[datetime] = _ts()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    speedup_factor: Mapped[float | None] = mapped_column(Float, nullable=True)


class QAReport(Base):
    __tablename__ = "qa_reports"

    id: Mapped[str] = _pk()
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    code_file: Mapped[str | None] = mapped_column(Text, nullable=True)
    tests_generated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tests_executed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tests_passed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tests_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tests_skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    coverage_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    diagnostics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    owner_user_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    created_at: Mapped[datetime] = _ts()


class Recipe(Base):
    __tablename__ = "recipes"

    id: Mapped[str] = _pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    task_dag_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    parameters_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    tags_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    executions_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    owner_user_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    created_at: Mapped[datetime] = _ts()


class AgentPersonality(Base):
    __tablename__ = "agent_personalities"

    id: Mapped[str] = _pk()
    agent_id: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    tone: Mapped[str] = mapped_column(String(20), default="formal", nullable=False)
    verbosity: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    reasoning_style: Mapped[str] = mapped_column(String(20), default="careful", nullable=False)
    system_prompt_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    learned_from_feedback_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = _ts()


class FeedbackLog(Base):
    __tablename__ = "feedback_log"

    id: Mapped[str] = _pk()
    agent_id: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    session_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _ts()


class WorkerAttempt(Base):
    """Durable fallback ledger (Agentic Runtime v1, additive).

    One row per provider attempt under the SAME task/worker. A provider
    switch changes only provider/model/attempt — the task id, worker id,
    tool state and memory stay put, so executed tool calls are never
    repeated just because the LLM changed.
    """

    __tablename__ = "worker_attempts"

    id: Mapped[str] = _pk()
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    worker_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model_id: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="started", nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tool_calls_made: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = _ts()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SwarmMember(Base):
    """Swarm membership: master-verified bounded fan-out (Agentic Runtime v1)."""

    __tablename__ = "swarm_members"

    id: Mapped[str] = _pk()
    master_task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    worker_task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(40), default="CODER", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    created_at: Mapped[datetime] = _ts()
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("worker_task_id", name="uq_swarm_members_worker_task_id"),)


class Insight(Base):
    __tablename__ = "insights"

    id: Mapped[str] = _pk()
    insight_type: Mapped[str] = mapped_column(String(24), nullable=False)
    generated_at: Mapped[datetime] = _ts()
    content_html: Mapped[str] = mapped_column(Text, nullable=False)
    key_findings_json: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    owner_user_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ScheduledJob(Base):
    __tablename__ = "scheduled_jobs"

    id: Mapped[str] = _pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)  # cron/interval/date/webhook
    schedule_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    owner_user_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    created_at: Mapped[datetime] = _ts()


class MemoryNote(Base):
    """Durable memory notes (cloud vault).

    Local dev persists notes as Obsidian markdown (services/memory.py);
    dynos have an ephemeral filesystem, so cloud deployments
    (``CLOUD_VAULT_DB=true``) persist the same scrubbed content here
    instead. Same fields as the markdown frontmatter + body.
    """

    __tablename__ = "memory_notes"

    id: Mapped[str] = _pk()
    title: Mapped[str] = mapped_column(Text, nullable=False)
    layer: Mapped[str] = mapped_column(String(20), default="TASK", nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(40), default="agent", nullable=False)
    tags_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    links_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    created_at: Mapped[datetime] = _ts()


# ---------------------------------------------------------------------------
# Cloud identity & transport (Telegram-first cloud runtime)
# ---------------------------------------------------------------------------


class User(Base):
    """A Bob user — the authorization principal (multi-user isolation).

    ``auth_provider`` records how the user is identified: ``telegram`` (a
    linked Telegram account) or ``local`` (the single-operator local runtime,
    where the web session secret *is* the operator identity). Telegram
    identity is never authorization: the :class:`Role` on
    :class:`TelegramAccount` decides what a Telegram principal may do.
    """

    __tablename__ = "users"

    id: Mapped[str] = _pk()
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    auth_provider: Mapped[str] = mapped_column(String(20), default="telegram", nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="member", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


class TelegramAccount(Base):
    """A Telegram identity linked to exactly one Bob user.

    Uniqueness on ``telegram_user_id`` makes the Telegram User -> Bob User
    mapping a function (one Telegram account can never map to two users).
    """

    __tablename__ = "telegram_accounts"

    id: Mapped[str] = _pk()
    telegram_user_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chat_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    role: Mapped[str] = mapped_column(String(20), default="member", nullable=False)
    linked_at: Mapped[datetime] = _ts()

    __table_args__ = (UniqueConstraint("telegram_user_id", name="uq_telegram_accounts_tg_user_id"),)


class TelegramUpdate(Base):
    """Ingest ledger + durable payload buffer for Telegram updates.

    One row per Telegram ``update_id``. The UNIQUE primary key is the
    idempotency gate: a retried webhook/poll delivery loses the INSERT race.
    ``processed_at IS NULL`` marks an ingested-but-not-yet-executed update —
    the reclaimable crash window. A retried delivery whose row is still
    ``processed_at IS NULL`` is re-processed (at-least-once work); a completed
    row (``processed_at`` set) is skipped. ``processed_at`` is written only
    after processing succeeds.
    """

    __tablename__ = "telegram_updates"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    account_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    chat_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime] = _ts()


class DeliveryOutbox(Base):
    """Durable outbound-notification queue (persist-first Telegram delivery).

    State is persisted BEFORE delivery is attempted (a Telegram message is
    never the record of record). Delivery loops claim rows with an atomic
    conditional UPDATE, back off exponentially on Telegram failures, cap
    total attempts (dead-letter), and reap claims stuck beyond the lease.
    """

    __tablename__ = "delivery_outbox"

    id: Mapped[str] = _pk()
    channel: Mapped[str] = mapped_column(String(20), default="telegram", nullable=False, index=True)
    kind: Mapped[str] = mapped_column(
        String(30), default="notification", nullable=False, index=True
    )
    chat_id: Mapped[str] = mapped_column(String(40), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    reply_markup_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=True)
    event_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    state: Mapped[str] = mapped_column(String(12), default="PENDING", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=True, index=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = _ts()
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Telegram Gateway E2E linkage (additive): which task produced this
    # message and which inbound message it replies to.
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    reply_to_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class TelegramGatewayMessage(Base):
    """First-class Telegram gateway state for one inbound update (additive).

    The durable pipeline: PERSIST the update -> ACK/PROCESS -> CREATE OR
    RESUME TASK -> ... -> DELIVER. This table is the update -> session ->
    task linkage that makes ingest idempotent (one Telegram update creates
    exactly one Bob task) and lets recovery find what a crashed dyno left
    half-done. ``telegram_updates`` (raw ingest ledger) remains untouched.
    """

    __tablename__ = "telegram_gateway_messages"
    __table_args__ = (UniqueConstraint("telegram_update_id", name="uq_tgm_update_id"),)

    id: Mapped[str] = _pk()
    telegram_update_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    received_at: Mapped[datetime] = _ts()
    session_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    processing_status: Mapped[str] = mapped_column(String(16), default="RECEIVED", nullable=False)


class UserCredential(Base):
    """Envelope-encrypted user credentials (v3.1 §14 & Chat-Native Credential Architecture).

    Stores encrypted secrets per user and provider/connection name.
    Raw secrets are NEVER stored unencrypted. Only metadata (provider, name,
    status, created_at, last_validated_at) is exposed to non-vault application logic.
    """

    __tablename__ = "user_credentials"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "provider", "name", name="uq_user_credentials_user_provider_name"
        ),
    )

    id: Mapped[str] = _pk()
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    encrypted_blob: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_dek: Mapped[str] = mapped_column(Text, nullable=False)
    encryption_algorithm: Mapped[str] = mapped_column(
        String(30), default="AES-256-GCM-ENVELOPE", nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default="healthy", nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()
    last_validated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class TelegramChatHistory(Base):
    """Conversational history for a Telegram chat (multi-turn context)."""

    __tablename__ = "telegram_chat_history"

    id: Mapped[str] = _pk()
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # "user" or "assistant"
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _ts(index=True)


class A2AProcessedEnvelope(Base):
    """Replay dedup for A2A callbacks: one envelope hash processed once."""

    __tablename__ = "a2a_processed_envelopes"

    envelope_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    delegation_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = _ts()
