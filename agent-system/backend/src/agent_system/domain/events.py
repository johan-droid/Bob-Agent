"""Canonical event system (v3.1 §6).

One event bus. Every important state transition produces an event with the
canonical envelope. No subsystem may create a second, incompatible bus, and no
subsystem may invent an event name at a call site: :func:`validate_event_type`
is the gate the bus applies to every emit.

Event taxonomy
--------------

1. **Canonical catalog** — the domains below (session, task, agent, model, tool,
   approval, workspace, artifact, qa, recovery, recipe, cost, insight, context).
   These are the events the product specification defines and the ones every
   consumer (trace UI, recordings, insights, realtime) may rely on.
2. **Registered extensions** — events an owning subsystem registers explicitly
   through :func:`register_event_extensions` (``skill.*``, ``a2a.*``,
   ``backup.*``). An extension is declared in one place with a named owner, so
   it is reviewable and discoverable, which is the whole point: the previous
   behaviour was arbitrary strings appearing at call sites.

Unknown types raise, which is intentional — a typo'd or invented event name is
an audit gap, not something to silently persist.
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
    "session.updated",
    "session.completed",
    "session.deleted",
    "session.blocked_user",
    "audit.denied",
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
    # Recorded whenever an unregistered agent type is routed to the generic
    # agent, so fallback routing is never invisible (v3.1 §4).
    "agent.fallback_applied",
)
MODEL_EVENTS = (
    "model.requested",
    "model.completed",
    "model.failed",
    # Streaming deltas ride the same bus as every other event (visibility=user)
    # so the chat UI, the CLI REPL and WS/SSE subscribers need no side channel.
    "model.token",
    # Provider circuit-breaker transitions (availability, not a model result).
    "model.circuit_opened",
    "model.circuit_closed",
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
    "workspace.restored",
)
ARTIFACT_EVENTS = (
    "artifact.created",
    "artifact.deleted",
)
CONTEXT_EVENTS = ("context.compacted",)
"""Context-window management events (v3.1 §11)."""
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
COST_EVENTS = (
    "cost.recorded",
    "cost.alert",
)
INSIGHT_EVENTS = ("insight.generated",)
ROUTER_EVENTS = (
    "router.selected",
    "router.fallback",
    "router.exhausted",
)
SWARM_EVENTS = (
    "swarm.created",
    "swarm.worker_created",
    "swarm.worker_completed",
    "swarm.verified",
)

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
    + CONTEXT_EVENTS
    + ROUTER_EVENTS
    + SWARM_EVENTS
)

# ---------------------------------------------------------------------------
# Extension mechanism
# ---------------------------------------------------------------------------

#: Subsystem-namespaced events, declared once here with their owner. A subsystem
#: that needs its own operational events registers them; producers then emit
#: normally and the bus accepts them.
_EVENT_EXTENSIONS: dict[str, str] = {}


class UnknownEventTypeError(ValueError):
    """Raised when a producer emits an event type outside the taxonomy."""

    def __init__(self, event_type: str) -> None:
        self.event_type = event_type
        super().__init__(
            f"unknown event type '{event_type}'. Canonical events: "
            f"{len(EVENT_TYPES)}; registered extensions: "
            f"{sorted(_EVENT_EXTENSIONS)}. Declare the event in "
            "domain/events.py (canonical catalog) or register it with "
            "register_event_extensions(owner, types) — never invent a name "
            "at the call site."
        )


def register_event_extensions(owner: str, types: tuple[str, ...]) -> None:
    """Register subsystem-owned operational events (v3.1 §6 extension point)."""
    for event_type in types:
        if event_type in EVENT_TYPES:
            raise ValueError(f"'{event_type}' is canonical; it cannot be re-registered")
        existing = _EVENT_EXTENSIONS.get(event_type)
        if existing is not None and existing != owner:
            raise ValueError(f"'{event_type}' is already registered by '{existing}'")
        _EVENT_EXTENSIONS[event_type] = owner


def event_extensions() -> dict[str, str]:
    """Registered extension events mapped to their owning subsystem."""
    return dict(_EVENT_EXTENSIONS)


def known_event_types() -> tuple[str, ...]:
    """Every event type the bus accepts (canonical catalog + extensions)."""
    return EVENT_TYPES + tuple(sorted(_EVENT_EXTENSIONS))


def validate_event_type(event_type: str) -> None:
    """Raise :class:`UnknownEventTypeError` for a type outside the taxonomy."""
    if event_type in EVENT_TYPES or event_type in _EVENT_EXTENSIONS:
        return
    raise UnknownEventTypeError(event_type)


# Extension declarations (owner -> events).
SKILL_EVENTS = ("skill.created", "skill.updated", "skill.deleted")
A2A_EVENTS = ("a2a.delegated", "a2a.result", "a2a.failed")
BACKUP_EVENTS = ("backup.completed", "backup.failed")

register_event_extensions("skills", SKILL_EVENTS)
register_event_extensions("a2a", A2A_EVENTS)
register_event_extensions("backup", BACKUP_EVENTS)


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
