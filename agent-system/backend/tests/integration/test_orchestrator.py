"""Integration tests — supervisor + orchestrator (v3.1 §9, Phase 4 acceptance)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_system.domain.events import utcnow
from agent_system.domain.tasks import TaskState
from agent_system.infra.db import make_engine, make_session_factory, session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import AgentLease, Base, Task
from agent_system.services.orchestrator import CycleError, Orchestrator, Supervisor


@pytest.fixture()
def env(tmp_path: Path) -> Iterator[tuple[object, object, EventBus]]:
    engine = make_engine(f"sqlite:///{tmp_path / 'orch.db'}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    bus = EventBus()
    yield factory, bus, bus
    engine.dispose()


def _supervisor(bus: EventBus) -> Supervisor:
    return Supervisor(bus)


def _orchestrator(bus: EventBus) -> Orchestrator:
    return Orchestrator(bus)


def test_session_and_task_creation(env: tuple[object, object, EventBus]) -> None:
    factory, bus, _ = env
    sup = _supervisor(bus)
    session_id = sup.create_session(factory, "build a thing")
    t1 = sup.add_task(factory, session_id, "code", "write code")
    t2 = sup.add_task(factory, session_id, "test", "test it", depends_on=[t1])
    assert t1.startswith("task_") and t2.startswith("task_")


def test_cycle_detection(env: tuple[object, object, EventBus]) -> None:
    factory, bus, _ = env
    sup = _supervisor(bus)
    session_id = sup.create_session(factory, "cycle")
    a = sup.add_task(factory, session_id, "code", "a")
    b = sup.add_task(factory, session_id, "code", "b", depends_on=[a])
    # Rewrite a to depend on b — creating a cycle.
    with session_scope(factory) as db:  # type: ignore[arg-type]
        db.get(Task, a).depends_on_json = [b]  # type: ignore[union-attr]
    with pytest.raises(CycleError):
        sup.plan(factory, session_id)


def test_dependency_ordering_and_execution(env: tuple[object, object, EventBus]) -> None:
    factory, bus, _ = env
    sup = _supervisor(bus)
    orch = _orchestrator(bus)
    execution_order: list[str] = []

    def code_handler(inp: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        execution_order.append("code")
        return {"done": True}

    def test_handler(inp: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        execution_order.append("test")
        return {"ok": True}

    orch.register_handler("code", code_handler)
    orch.register_handler("test", test_handler)

    session_id = sup.create_session(factory, "ordered")
    t1 = sup.add_task(factory, session_id, "code", "first")
    t2 = sup.add_task(factory, session_id, "test", "second", depends_on=[t1])

    sup.plan(factory, session_id)
    orch.run_ready_tasks(factory, session_id)  # runs t1
    orch.run_ready_tasks(factory, session_id)  # now t2 is ready
    assert execution_order == ["code", "test"]

    with session_scope(factory) as db:  # type: ignore[arg-type]
        assert db.get(Task, t1).state == TaskState.SUCCEEDED.value  # type: ignore[union-attr]
        assert db.get(Task, t2).state == TaskState.SUCCEEDED.value  # type: ignore[union-attr]


def test_failed_dependency_blocks_dependent(env: tuple[object, object, EventBus]) -> None:
    factory, bus, _ = env
    sup = _supervisor(bus)
    orch = _orchestrator(bus)

    def failing(inp: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("boom")

    orch.register_handler("code", failing)
    orch.register_handler("test", lambda i, c: {"ok": True})

    session_id = sup.create_session(factory, "failure blocks")
    t1 = sup.add_task(factory, session_id, "code", "will fail")
    t2 = sup.add_task(factory, session_id, "test", "never runs", depends_on=[t1])
    sup.plan(factory, session_id)
    orch.run_ready_tasks(factory, session_id)
    orch.run_ready_tasks(factory, session_id)

    with session_scope(factory) as db:  # type: ignore[arg-type]
        assert db.get(Task, t1).state == TaskState.FAILED.value  # type: ignore[union-attr]
        # Dependent of a failed task is never queued — stays PENDING forever
        # until an operator retries the failed dependency.
        assert db.get(Task, t2).state == TaskState.PENDING.value  # type: ignore[union-attr]


def test_unknown_handler_fails_task_cleanly(env: tuple[object, object, EventBus]) -> None:
    factory, bus, _ = env
    sup = _supervisor(bus)
    orch = _orchestrator(bus)
    session_id = sup.create_session(factory, "no handler")
    task_id = sup.add_task(factory, session_id, "mystery", "unhandled")
    sup.plan(factory, session_id)
    orch.run_ready_tasks(factory, session_id)
    with session_scope(factory) as db:  # type: ignore[arg-type]
        task = db.get(Task, task_id)
        assert task.state == TaskState.FAILED.value  # type: ignore[union-attr]
        assert "no handler" in (task.last_error or "")  # type: ignore[union-attr]


def test_cancel_queued_task(env: tuple[object, object, EventBus]) -> None:
    factory, bus, _ = env
    sup = _supervisor(bus)
    orch = _orchestrator(bus)
    session_id = sup.create_session(factory, "cancel")
    task_id = sup.add_task(factory, session_id, "code", "cancel me")
    sup.plan(factory, session_id)
    assert orch.cancel_task(factory, task_id)
    with session_scope(factory) as db:  # type: ignore[arg-type]
        assert db.get(Task, task_id).state == TaskState.CANCELLED.value  # type: ignore[union-attr]


def test_stale_lease_recovery_requeues(env: tuple[object, object, EventBus]) -> None:
    """Simulated crash: task RUNNING, lease expired -> requeued; attempt keeps
    the value the worker set at RUNNING (reaper does not double-increment)."""
    factory, bus, _ = env
    sup = _supervisor(bus)
    orch = _orchestrator(bus)
    session_id = sup.create_session(factory, "crash")
    task_id = sup.add_task(factory, session_id, "code", "crashed task")

    # Force the task into RUNNING with an expired lease (as after a crash):
    with session_scope(factory) as db:  # type: ignore[arg-type]
        task = db.get(Task, task_id)
        task.state = TaskState.RUNNING.value  # type: ignore[union-attr]
        task.attempt = 1  # worker sets this at the RUNNING transition
        run_id = "run_test_crash"
        from agent_system.infra.models import AgentRun

        db.add(
            AgentRun(
                id=run_id,
                task_id=task_id,
                agent_type="code",
                state="RUNNING",
                worker_id="dead-worker",
            )
        )
        db.add(
            AgentLease(
                agent_run_id=run_id,
                worker_id="dead-worker",
                state="RUNNING",
                heartbeat_at=utcnow() - timedelta(minutes=5),
                lease_expires_at=utcnow() - timedelta(minutes=4),
            )
        )

    recovered = orch.recover_orphans(factory)
    assert task_id in recovered
    with session_scope(factory) as db:  # type: ignore[arg-type]
        task = db.get(Task, task_id)
        assert task.state == TaskState.QUEUED.value  # type: ignore[union-attr]
        assert task.attempt == 1  # type: ignore[union-attr]


def test_stale_lease_recovery_fails_after_retries_exhausted(
    env: tuple[object, object, EventBus],
) -> None:
    factory, bus, _ = env
    sup = _supervisor(bus)
    orch = _orchestrator(bus)
    session_id = sup.create_session(factory, "exhausted")
    task_id = sup.add_task(factory, session_id, "code", "doomed")

    with session_scope(factory) as db:  # type: ignore[arg-type]
        task = db.get(Task, task_id)
        task.state = TaskState.RUNNING.value  # type: ignore[union-attr]
        task.attempt = 3  # retries already exhausted
        run_id = "run_doomed"
        from agent_system.infra.models import AgentRun

        db.add(
            AgentRun(
                id=run_id, task_id=task_id, agent_type="code", state="RUNNING", worker_id="dead"
            )
        )
        db.add(
            AgentLease(
                agent_run_id=run_id,
                worker_id="dead",
                state="RUNNING",
                heartbeat_at=utcnow() - timedelta(minutes=5),
                lease_expires_at=utcnow() - timedelta(minutes=4),
            )
        )

    recovered = orch.recover_orphans(factory)
    assert task_id in recovered
    with session_scope(factory) as db:  # type: ignore[arg-type]
        task = db.get(Task, task_id)
        assert task.state == TaskState.FAILED.value  # type: ignore[union-attr]
        assert "retries exhausted" in (task.last_error or "")  # type: ignore[union-attr]


def test_task_limit_enforced(env: tuple[object, object, EventBus]) -> None:
    factory, bus, _ = env
    sup = _supervisor(bus)
    session_id = sup.create_session(factory, "limit")
    with pytest.raises(ValueError, match="limit"):
        for _ in range(101):
            sup.add_task(factory, session_id, "code", "t")
