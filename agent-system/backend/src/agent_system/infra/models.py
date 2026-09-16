"""ORM models for the canonical domain (v3.1 §5 + v3.0 §7 tables).

JSON columns hold structured payloads validated by Pydantic at service
boundaries. All IDs are prefixed ULID strings (see domain.ids).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
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


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# ---------------------------------------------------------------------------
# Core orchestration
# ---------------------------------------------------------------------------


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = _pk()
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
    __tablename__ = "approvals"

    id: Mapped[str] = _pk()
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    requested_action: Mapped[str] = mapped_column(Text, nullable=False)
    risk: Mapped[str] = mapped_column(String(12), nullable=False)  # LOW/MEDIUM/HIGH/CRITICAL
    scope: Mapped[str] = mapped_column(String(60), nullable=False)
    requester: Mapped[str] = mapped_column(String(60), nullable=False)
    decision: Mapped[str] = mapped_column(String(12), default="PENDING", nullable=False, index=True)
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
    created_at: Mapped[datetime] = _ts()
    last_modified_at: Mapped[datetime] = _ts()


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = _pk()
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # pptx/pdf/docx/xlsx/file
    path: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
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
    __tablename__ = "model_calls"

    id: Mapped[str] = _pk()
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
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
    created_at: Mapped[datetime] = _ts()


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


class Insight(Base):
    __tablename__ = "insights"

    id: Mapped[str] = _pk()
    insight_type: Mapped[str] = mapped_column(String(24), nullable=False)
    generated_at: Mapped[datetime] = _ts()
    content_html: Mapped[str] = mapped_column(Text, nullable=False)
    key_findings_json: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ScheduledJob(Base):
    __tablename__ = "scheduled_jobs"

    id: Mapped[str] = _pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)  # cron/interval/date/webhook
    schedule_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
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
    created_at: Mapped[datetime] = _ts()
