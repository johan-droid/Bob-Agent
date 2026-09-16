"""Task state machine (v3.1 §7).

Allowed states and explicit transitions. Invalid transitions are rejected —
never silently coerced. Every transition must also be persisted and evented by
the caller (services layer), this module only owns the validity rules.
"""

from __future__ import annotations

from enum import StrEnum


class TaskState(StrEnum):
    PENDING = "PENDING"
    PLANNING = "PLANNING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    BLOCKED_APPROVAL = "BLOCKED_APPROVAL"
    RECOVERING = "RECOVERING"
    REVIEW = "REVIEW"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# Explicit transition table: state -> set of allowed next states
TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset({TaskState.PLANNING, TaskState.QUEUED, TaskState.CANCELLED}),
    TaskState.PLANNING: frozenset({TaskState.QUEUED, TaskState.FAILED, TaskState.CANCELLED}),
    TaskState.QUEUED: frozenset({TaskState.RUNNING, TaskState.CANCELLED}),
    TaskState.RUNNING: frozenset(
        {
            TaskState.BLOCKED_APPROVAL,
            TaskState.RECOVERING,
            TaskState.REVIEW,
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.BLOCKED_APPROVAL: frozenset(
        {
            TaskState.RUNNING,  # approved
            TaskState.FAILED,  # denied
            TaskState.CANCELLED,
        }
    ),
    TaskState.RECOVERING: frozenset(
        {
            TaskState.QUEUED,  # retry scheduled
            TaskState.RUNNING,  # resumed in place
            TaskState.FAILED,  # recovery exhausted
            TaskState.CANCELLED,
        }
    ),
    TaskState.REVIEW: frozenset({TaskState.SUCCEEDED, TaskState.FAILED, TaskState.RUNNING}),
    TaskState.SUCCEEDED: frozenset(),
    TaskState.FAILED: frozenset({TaskState.QUEUED, TaskState.RECOVERING}),  # explicit retry only
    TaskState.CANCELLED: frozenset(),
}

TERMINAL_STATES = frozenset({TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED})


class InvalidTransitionError(ValueError):
    """Raised when a task state transition is not allowed."""

    def __init__(self, current: TaskState, target: TaskState) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Invalid task transition: {current.value} -> {target.value}")


def validate_transition(current: TaskState, target: TaskState) -> None:
    """Raise InvalidTransitionError if current -> target is not allowed."""
    if target not in TRANSITIONS[current]:
        raise InvalidTransitionError(current, target)


def can_transition(current: TaskState, target: TaskState) -> bool:
    try:
        validate_transition(current, target)
    except InvalidTransitionError:
        return False
    return True
