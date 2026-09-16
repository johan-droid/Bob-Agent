"""Integration tests: RQ worker execution + lease/heartbeat recovery (Phase 4).

Redis is required (docker compose up -d). Tests drive `execute_task` directly
(function entrypoint — the same code RQ runs) plus the real reaper, and
verify kill-style orphan recovery via expired leases.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from redis import Redis

from agent_system.config import get_settings
from agent_system.domain.events import utcnow
from agent_system.infra.db import make_engine, make_session_factory, session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import AgentLease, Base, Task
from agent_system.services.orchestrator import Orchestrator
from agent_system.worker import LEASE_TTL_SECONDS, execute_task

redis_url = get_settings().redis_url


def _redis_available() -> bool:
    try:
        Redis.from_url(redis_url).ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _redis_available(), reason="redis not running")


@pytest.fixture()
def factory(tmp_path: Any) -> Any:
    engine = make_engine(f"sqlite:///{tmp_path / 'w.db'}")
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture()
def bus() -> EventBus:
    return EventBus()


def _queued_task(factory: Any, bus: EventBus, session_id: str = "ses_test") -> str:
    with session_scope(factory) as db:
        db.execute = db.execute  # noqa: PLW0127 — keep type checkers honest
        from agent_system.infra.models import Session

        db.add(Session(id=session_id, goal="g", status="ACTIVE"))
        task_id = "task_testworker000000000000"
        db.add(
            Task(
                id=task_id,
                session_id=session_id,
                task_type="demo",
                title="t",
                state="QUEUED",
            )
        )
        bus.emit(
            __import__("agent_system.domain.events", fromlist=["Event"]).Event(
                type="task.queued", session_id=session_id, task_id=task_id, actor="test"
            ),
            db,
        )
    return task_id


def test_execute_task_succeeds_and_persists(factory: Any, bus: EventBus) -> None:
    task_id = _queued_task(factory, bus)
    result = execute_task(task_id, factory=factory)
    assert result["state"] == "SUCCEEDED"
    with session_scope(factory) as db:
        task = db.get(Task, task_id)
        assert task is not None
        assert task.state == "SUCCEEDED"
        assert task.result_json is not None
        assert db.query(AgentLease).count() == 0  # lease released


def test_execute_task_skips_nonqueued(factory: Any, bus: EventBus) -> None:
    task_id = _queued_task(factory, bus)
    with session_scope(factory) as db:
        db.get(Task, task_id).state = "CANCELLED"
    result = execute_task(task_id, factory=factory)
    assert result.get("skipped") is True


def test_stale_lease_recovered_by_reaper(factory: Any, bus: EventBus) -> None:
    """kill -9 simulation: RUNNING task with an expired lease gets requeued.

    attempt counts "times execution started" — the worker incremented it at
    RUNNING before crashing; the reaper must NOT increment again.
    """
    task_id = _queued_task(factory, bus)
    with session_scope(factory) as db:
        task = db.get(Task, task_id)
        task.state = "RUNNING"  # simulate worker died mid-task
        task.attempt = 1  # set by the worker at the RUNNING transition
        from agent_system.infra.models import AgentRun

        run_id = "run_stale00000000000000001"
        db.add(AgentRun(id=run_id, task_id=task_id, agent_type="demo", state="RUNNING"))
        db.add(
            AgentLease(
                agent_run_id=run_id,
                worker_id="dead-worker",
                state="RUNNING",
                heartbeat_at=utcnow() - timedelta(seconds=LEASE_TTL_SECONDS * 4),
                lease_expires_at=utcnow() - timedelta(seconds=LEASE_TTL_SECONDS * 2),
            )
        )
    orch = Orchestrator(event_bus=bus)
    recovered = orch.recover_orphans(factory)
    assert recovered == [task_id]
    with session_scope(factory) as db:
        task = db.get(Task, task_id)
        assert task.state == "QUEUED"
        assert task.attempt == 1


def test_exhausted_retries_fail_after_stale_lease(factory: Any, bus: EventBus) -> None:
    task_id = _queued_task(factory, bus)
    with session_scope(factory) as db:
        task = db.get(Task, task_id)
        task.state = "RUNNING"
        task.attempt = 3  # at max retries
        from agent_system.infra.models import AgentRun

        run_id = "run_stale00000000000000002"
        db.add(AgentRun(id=run_id, task_id=task_id, agent_type="demo", state="RUNNING"))
        db.add(
            AgentLease(
                agent_run_id=run_id,
                worker_id="dead-worker",
                state="RUNNING",
                heartbeat_at=utcnow() - timedelta(seconds=LEASE_TTL_SECONDS * 4),
                lease_expires_at=utcnow() - timedelta(seconds=LEASE_TTL_SECONDS * 2),
            )
        )
    orch = Orchestrator(event_bus=bus)
    assert orch.recover_orphans(factory) == [task_id]
    with session_scope(factory) as db:
        task = db.get(Task, task_id)
        assert task.state == "FAILED"


def test_failed_task_records_error(factory: Any, bus: EventBus) -> None:
    from agent_system.agents import registry

    def boom(task_input: dict, context: dict) -> dict:
        raise RuntimeError("exploded")

    registry.register("explode", boom)
    task_id = _queued_task(factory, bus)
    with session_scope(factory) as db:
        db.get(Task, task_id).task_type = "explode"
    with pytest.raises(RuntimeError):
        execute_task(task_id, factory=factory)
    with session_scope(factory) as db:
        task = db.get(Task, task_id)
        assert task.state == "FAILED"
        assert "exploded" in task.last_error
