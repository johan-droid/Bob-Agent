"""Canonical event system (v3.1 §6).

One event bus. Every important state transition produces an event with the
canonical envelope. No subsystem may create a second, incompatible bus.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from agent_system.domain.ids import new_event_id

# ---------------------------------------------------------------------------
# Canonical event types
# ---------------------------------------------------------------------------

SESSION_EVENTS = (
    "session.created",
    "session.completed",
)
TASK_EVENTS = (
    "task.created",
    "task.queued",
    "task.started",
    "task.completed",
    "task.failed",
    "task.cancelled",
    "task.recovering",
    "task.blocked_approval",
)
AGENT_EVENTS = (
    "agent.created",
    "agent.started",
    "agent.waiting_tool",
    "agent.waiting_approval",
    "agent.completed",
    "agent.failed",
    "agent.terminated",
)
MODEL_EVENTS = (
    "model.requested",
    "model.completed",
    "model.failed",
)
TOOL_EVENTS = (
    "tool.started",
    "tool.completed",
    "tool.failed",
)
APPROVAL_EVENTS = (
    "approval.requested",
    "approval.approved",
    "approval.denied",
    "approval.expired",
)
WORKSPACE_EVENTS = (
    "workspace.created",
    "workspace.modified",
    "workspace.destroyed",
)
ARTIFACT_EVENTS = (
    "artifact.created",
    "artifact.deleted",
)
QA_EVENTS = (
    "qa.started",
    "qa.completed",
    "qa.failed",
)
RECOVERY_EVENTS = (
    "recovery.started",
    "recovery.completed",
    "recovery.failed",
)
RECIPE_EVENTS = (
    "recipe.started",
    "recipe.completed",
    "recipe.failed",
)
COST_EVENTS = ("cost.recorded",)
INSIGHT_EVENTS = ("insight.generated",)

EVENT_TYPES: tuple[str, ...] = (
    SESSION_EVENTS
    + TASK_EVENTS
    + AGENT_EVENTS
    + MODEL_EVENTS
    + TOOL_EVENTS
    + APPROVAL_EVENTS
    + WORKSPACE_EVENTS
    + ARTIFACT_EVENTS
    + QA_EVENTS
    + RECOVERY_EVENTS
    + RECIPE_EVENTS
    + COST_EVENTS
    + INSIGHT_EVENTS
)


class EventVisibility(StrEnum):
    USER = "user"
    INTERNAL = "internal"


class EventSensitivity(StrEnum):
    NORMAL = "normal"
    SENSITIVE = "sensitive"


def utcnow() -> datetime:
    return datetime.now(UTC)


class Event(BaseModel):
    """Canonical event envelope (v3.1 §6)."""

    event_id: str = Field(default_factory=new_event_id)
    schema_version: int = 1
    session_id: str | None = None
    task_id: str | None = None
    agent_run_id: str | None = None
    sequence: int | None = None  # assigned by the EventBus on persist
    timestamp: datetime = Field(default_factory=utcnow)
    type: str
    actor: str = "system"
    payload: dict[str, Any] = Field(default_factory=dict)
    visibility: EventVisibility = EventVisibility.USER
    sensitivity: EventSensitivity = EventSensitivity.NORMAL

    def with_sequence(self, sequence: int) -> Event:
        """Return a copy of this event with its sequence number assigned."""
        return self.model_copy(update={"sequence": sequence})
