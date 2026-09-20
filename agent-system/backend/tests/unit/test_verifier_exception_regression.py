"""Regression tests: verifier exceptions MUST mark tasks as failed, never passed.

Requirement 2:
- NEVER treat verifier exceptions as successful verification.
- If verification crashes, mark the task as failed/blocked/retryable with an explicit reason.
- Preserve the existing task state machine.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from agent_system.domain.tasks import TaskState
from agent_system.infra.db import make_engine, make_session_factory
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Base, Task
from agent_system.services.orchestrator import Orchestrator, Supervisor
from agent_system.services.verifier import Verifier
from agent_system.worker import execute_task


def _setup_db(tmp_path: Any) -> Any:
    engine = make_engine(f"sqlite:///{tmp_path / 'test_verifier.db'}")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def test_verifier_exception_returns_failed_result() -> None:
    verifier = Verifier()
    with patch.object(verifier, "_check_deterministic", side_effect=RuntimeError("internal crash")):
        res = verifier.verify({"goal": "test"}, {"result": "ok"})
        assert res.passed is False
        assert "verifier crashed" in res.reason
        assert "RuntimeError" in res.reason


def test_orchestrator_verifier_crash_fails_task(tmp_path: Any) -> None:
    factory = _setup_db(tmp_path)
    bus = EventBus()
    sup = Supervisor(bus)
    session_id = sup.create_session(factory, "test verifier exception")
    task_id = sup.add_task(factory, session_id, "llm", "llm task")

    # Queue task
    sup.plan(factory, session_id)

    # Broken verifier that raises an exception
    mock_verifier = MagicMock()
    mock_verifier.verify.side_effect = RuntimeError("verifier exploded")

    orch = Orchestrator(bus, verifier=mock_verifier)
    orch.register_handler("llm", lambda task_input, ctx: {"status": "ok"})

    started = orch.run_ready_tasks(factory, session_id)
    assert task_id in started

    with factory() as db:
        task = db.get(Task, task_id)
        assert task is not None
        assert task.state == TaskState.FAILED.value
        assert "verifier crashed" in task.last_error


def test_worker_verifier_crash_fails_task(tmp_path: Any) -> None:
    factory = _setup_db(tmp_path)
    bus = EventBus()
    sup = Supervisor(bus)
    session_id = sup.create_session(factory, "test worker verifier crash")
    task_id = sup.add_task(factory, session_id, "llm", "llm task")

    sup.plan(factory, session_id)

    with patch(
        "agent_system.services.verifier.Verifier.verify",
        side_effect=RuntimeError("worker verifier crash"),
    ):
        with patch("agent_system.agents.react_agent.install"):
            with patch("agent_system.agents.registry.run_agent", return_value={"output": "hello"}):
                res = execute_task(task_id, factory=factory)
                assert res["state"] == "FAILED"

    with factory() as db:
        task = db.get(Task, task_id)
        assert task is not None
        assert task.state == TaskState.FAILED.value
        assert "verifier crashed" in task.last_error
