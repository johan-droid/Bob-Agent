"""Agent lifecycle (v3.1 §8).

Agent states with explicit transitions. Heartbeat/lease tracking lives in the
infra layer; this module owns the state rules.
"""

from __future__ import annotations

from enum import StrEnum


class AgentState(StrEnum):
    CREATED = "CREATED"
    INITIALIZING = "INITIALIZING"
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING_TOOL = "WAITING_TOOL"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    RECOVERING = "RECOVERING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TERMINATED = "TERMINATED"


TRANSITIONS: dict[AgentState, frozenset[AgentState]] = {
    AgentState.CREATED: frozenset({AgentState.INITIALIZING, AgentState.TERMINATED}),
    AgentState.INITIALIZING: frozenset(
        {AgentState.READY, AgentState.FAILED, AgentState.TERMINATED}
    ),
    AgentState.READY: frozenset({AgentState.RUNNING, AgentState.TERMINATED}),
    AgentState.RUNNING: frozenset(
        {
            AgentState.WAITING_TOOL,
            AgentState.WAITING_APPROVAL,
            AgentState.RECOVERING,
            AgentState.COMPLETED,
            AgentState.FAILED,
            AgentState.TERMINATED,
        }
    ),
    AgentState.WAITING_TOOL: frozenset(
        {AgentState.RUNNING, AgentState.RECOVERING, AgentState.FAILED, AgentState.TERMINATED}
    ),
    AgentState.WAITING_APPROVAL: frozenset(
        {AgentState.RUNNING, AgentState.FAILED, AgentState.TERMINATED}
    ),
    AgentState.RECOVERING: frozenset(
        {AgentState.RUNNING, AgentState.FAILED, AgentState.TERMINATED}
    ),
    AgentState.COMPLETED: frozenset(),
    AgentState.FAILED: frozenset({AgentState.RECOVERING}),  # explicit recovery only
    AgentState.TERMINATED: frozenset(),
}


class InvalidAgentTransitionError(ValueError):
    def __init__(self, current: AgentState, target: AgentState) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Invalid agent transition: {current.value} -> {target.value}")


def validate_transition(current: AgentState, target: AgentState) -> None:
    if target not in TRANSITIONS[current]:
        raise InvalidAgentTransitionError(current, target)


def can_transition(current: AgentState, target: AgentState) -> bool:
    try:
        validate_transition(current, target)
    except InvalidAgentTransitionError:
        return False
    return True
