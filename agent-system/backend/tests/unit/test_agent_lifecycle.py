"""Unit tests — agent lifecycle (v3.1 §8)."""

from __future__ import annotations

import pytest

from agent_system.domain.lifecycles import (
    TRANSITIONS,
    AgentState,
    InvalidAgentTransitionError,
    validate_transition,
)


def test_normal_lifecycle() -> None:
    path = [
        AgentState.CREATED,
        AgentState.INITIALIZING,
        AgentState.READY,
        AgentState.RUNNING,
        AgentState.COMPLETED,
    ]
    for current, target in zip(path, path[1:], strict=False):
        validate_transition(current, target)


def test_waiting_states_resume() -> None:
    validate_transition(AgentState.RUNNING, AgentState.WAITING_TOOL)
    validate_transition(AgentState.WAITING_TOOL, AgentState.RUNNING)
    validate_transition(AgentState.RUNNING, AgentState.WAITING_APPROVAL)
    validate_transition(AgentState.WAITING_APPROVAL, AgentState.RUNNING)


def test_no_zombie_running_after_terminate() -> None:
    validate_transition(AgentState.RUNNING, AgentState.TERMINATED)
    with pytest.raises(InvalidAgentTransitionError):
        validate_transition(AgentState.TERMINATED, AgentState.RUNNING)


def test_recovery_path() -> None:
    validate_transition(AgentState.RUNNING, AgentState.RECOVERING)
    validate_transition(AgentState.RECOVERING, AgentState.RUNNING)
    validate_transition(AgentState.RECOVERING, AgentState.FAILED)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (AgentState.CREATED, AgentState.RUNNING),  # must initialize first
        (AgentState.COMPLETED, AgentState.RUNNING),  # terminal
        (AgentState.FAILED, AgentState.RUNNING),  # must go through RECOVERING
    ],
)
def test_invalid(current: AgentState, target: AgentState) -> None:
    with pytest.raises(InvalidAgentTransitionError):
        validate_transition(current, target)


def test_terminal_states_have_no_outgoing() -> None:
    for state in (AgentState.COMPLETED, AgentState.TERMINATED):
        assert len(TRANSITIONS[state]) == 0
