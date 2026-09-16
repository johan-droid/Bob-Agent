"""Unit tests — task state machine (v3.1 §7)."""

from __future__ import annotations

import pytest

from agent_system.domain.tasks import (
    TRANSITIONS,
    InvalidTransitionError,
    TaskState,
    can_transition,
    validate_transition,
)


class TestHappyPath:
    def test_full_lifecycle(self) -> None:
        path = [
            TaskState.PENDING,
            TaskState.PLANNING,
            TaskState.QUEUED,
            TaskState.RUNNING,
            TaskState.SUCCEEDED,
        ]
        for current, target in zip(path, path[1:], strict=False):
            validate_transition(current, target)

    def test_approval_block_and_resume(self) -> None:
        validate_transition(TaskState.RUNNING, TaskState.BLOCKED_APPROVAL)
        validate_transition(TaskState.BLOCKED_APPROVAL, TaskState.RUNNING)

    def test_denied_approval_fails_task(self) -> None:
        validate_transition(TaskState.BLOCKED_APPROVAL, TaskState.FAILED)

    def test_recovery_paths(self) -> None:
        validate_transition(TaskState.RUNNING, TaskState.RECOVERING)
        validate_transition(TaskState.RECOVERING, TaskState.QUEUED)
        validate_transition(TaskState.RECOVERING, TaskState.RUNNING)
        validate_transition(TaskState.RECOVERING, TaskState.FAILED)


class TestInvalidTransitions:
    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (TaskState.PENDING, TaskState.RUNNING),  # must pass through QUEUED
            (TaskState.PENDING, TaskState.SUCCEEDED),  # no instant success
            (TaskState.QUEUED, TaskState.SUCCEEDED),  # must actually run
            (TaskState.QUEUED, TaskState.PLANNING),  # backwards
            (TaskState.SUCCEEDED, TaskState.RUNNING),  # terminal
            (TaskState.CANCELLED, TaskState.QUEUED),  # terminal
            (TaskState.FAILED, TaskState.SUCCEEDED),  # must retry via QUEUED/RECOVERING
            (TaskState.RUNNING, TaskState.PENDING),  # backwards
            (TaskState.BLOCKED_APPROVAL, TaskState.SUCCEEDED),  # must resume first
        ],
    )
    def test_rejects_invalid(self, current: TaskState, target: TaskState) -> None:
        with pytest.raises(InvalidTransitionError):
            validate_transition(current, target)
        assert not can_transition(current, target)


class TestInvariants:
    def test_fully_terminal_states_have_no_outgoing(self) -> None:
        # SUCCEEDED and CANCELLED are final; FAILED allows explicit retry
        # (v3.1 §7: retry is a deliberate operator action via QUEUED/RECOVERING,
        # never automatic).
        for state in (TaskState.SUCCEEDED, TaskState.CANCELLED):
            assert len(TRANSITIONS[state]) == 0

    def test_failed_retry_is_explicit_and_limited(self) -> None:
        targets = TRANSITIONS[TaskState.FAILED]
        assert targets == frozenset({TaskState.QUEUED, TaskState.RECOVERING})

    def test_every_state_has_transition_entry(self) -> None:
        for state in TaskState:
            assert state in TRANSITIONS

    def test_all_transition_targets_are_valid_states(self) -> None:
        for targets in TRANSITIONS.values():
            for target in targets:
                assert isinstance(target, TaskState)
