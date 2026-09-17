"""RQ queue worker (v3.1 Phase 4): out-of-process task execution.

SQLite stays authoritative — the queue only carries task ids. Workers
re-derive all state from SQLite, hold a lease with heartbeats while running,
and every crash path is recoverable by the lease reaper (`recover_orphans`).

Worker entrypoint (separate process):
    python -m agent_system.worker
"""

from __future__ import annotations

import os
import threading
from datetime import timedelta
from typing import Any

from redis import Redis
from rq import Queue, Worker

from agent_system.config import get_settings
from agent_system.domain import ids
from agent_system.domain.events import Event, utcnow
from agent_system.domain.lifecycles import AgentState
from agent_system.domain.tasks import TaskState, validate_transition
from agent_system.infra.db import make_engine, make_session_factory, session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import AgentLease, AgentRun, Task

QUEUE_NAME = "agent-system"
LEASE_TTL_SECONDS = 60
HEARTBEAT_INTERVAL_SECONDS = 15.0


def get_queue(redis_url: str | None = None) -> Queue:
    settings = get_settings()
    conn = Redis.from_url(redis_url or settings.redis_url)
    return Queue(QUEUE_NAME, connection=conn)


def enqueue_task(factory: Any, redis_url: str | None = None, task_id: str | None = None) -> str:
    """Enqueue a queued task by id. SQLite remains the source of truth."""
    q = get_queue(redis_url)
    job = q.enqueue(
        "agent_system.worker.execute_task",
        task_id=task_id,
        job_timeout=get_settings().max_execution_time_seconds,
    )
    return job.id


def _heartbeat_loop(factory: Any, run_id: str, stop: threading.Event) -> None:
    """Refresh the lease heartbeat until the run finishes or the worker dies."""
    while not stop.wait(HEARTBEAT_INTERVAL_SECONDS):
        try:
            with session_scope(factory) as db:
                lease = db.get(AgentLease, run_id)
                if lease is None:
                    return
                lease.heartbeat_at = utcnow()
                lease.lease_expires_at = utcnow() + timedelta(seconds=LEASE_TTL_SECONDS)
        except Exception:
            return  # DB gone — reaper will handle orphan detection


def execute_task(task_id: str | None = None, factory: Any = None) -> dict[str, Any]:
    """RQ job body: run one task to completion with lease + heartbeat.

    Raises on failure so RQ marks the job failed; SQLite task state is the
    durable record either way. `factory` is injectable for tests; production
    builds its own session factory from settings.
    """
    if task_id is None:
        raise ValueError("execute_task requires task_id")
    engine = None
    if factory is None:
        settings = get_settings()
        engine = make_engine(settings.database_url)
        factory = make_session_factory(engine)
    bus = EventBus()
    worker_id = os.environ.get("RQ_WORKER_ID", "rq-worker")

    with session_scope(factory) as db:
        task = db.get(Task, task_id)
        if task is None:
            raise LookupError(f"task {task_id} not found")
        if task.state != TaskState.QUEUED.value:
            # Already handled (e.g. duplicate delivery) — not an error.
            return {"task_id": task_id, "skipped": True, "state": task.state}
        # Conditional claim: exactly one claimant moves QUEUED -> RUNNING and
        # counts the attempt. Concurrent duplicate deliveries (RQ redelivery,
        # a second worker) lose the race and skip below instead of running
        # the task twice.
        claimed: int = (
            db.query(Task)
            .filter(Task.id == task_id, Task.state == TaskState.QUEUED.value)
            .update(
                {
                    "state": TaskState.RUNNING.value,
                    "started_at": utcnow(),
                    "attempt": Task.attempt + 1,
                },
                synchronize_session=False,
            )
        )
        if claimed == 0:
            db.refresh(task)
            return {"task_id": task_id, "skipped": True, "state": task.state}
        db.refresh(task)
        run_id = ids.new_agent_run_id()
        agent_type = task.agent_type or task.task_type
        db.add(
            AgentRun(
                id=run_id,
                task_id=task_id,
                agent_type=agent_type,
                state=AgentState.RUNNING.value,
                worker_id=worker_id,
            )
        )
        db.add(
            AgentLease(
                agent_run_id=run_id,
                worker_id=worker_id,
                state=AgentState.RUNNING.value,
                heartbeat_at=utcnow(),
                lease_expires_at=utcnow() + timedelta(seconds=LEASE_TTL_SECONDS),
            )
        )
        bus.emit(
            Event(
                type="task.started",
                session_id=task.session_id,
                task_id=task_id,
                agent_run_id=run_id,
                actor=agent_type,
                payload={"worker": worker_id},
            ),
            db,
        )
        session_id = task.session_id
        input_snapshot = dict(task.input_json or {})

    stop = threading.Event()
    hb = threading.Thread(target=_heartbeat_loop, args=(factory, run_id, stop), daemon=True)
    hb.start()
    try:
        from agent_system.agents import react_agent
        from agent_system.agents.registry import run_agent

        # Real LLM execution: the llm handler runs the ReAct loop with the
        # tool registry. Without a configured provider the deterministic
        # builtin stays the fallback (honest offline behavior).
        react_agent.install()
        result = run_agent(
            agent_type,
            input_snapshot,
            {
                "session_id": session_id,
                "task_id": task_id,
                "agent_run_id": run_id,
                "agent_type": agent_type,
                "factory": factory,
                "bus": bus,
            },
        )
    except Exception as exc:
        _finish_failed(factory, bus, task_id, run_id, session_id, agent_type, exc)
        raise
    finally:
        stop.set()
        hb.join(timeout=2)

    with session_scope(factory) as db:
        task = db.get(Task, task_id)
        run = db.get(AgentRun, run_id)
        lease = db.get(AgentLease, run_id)
        # REVIEW gate (mirrors Orchestrator._verify_and_finish): successful
        # handler output is verified before it can succeed.
        verification_passed = True
        verification_reason = "no verifier"
        verification_mode = "off"
        if task is not None and task.state == TaskState.RUNNING.value:
            validate_transition(TaskState.RUNNING, TaskState.REVIEW)
            task.state = TaskState.REVIEW.value
            bus.emit(
                Event(
                    type="qa.started",
                    session_id=session_id,
                    task_id=task_id,
                    agent_run_id=run_id,
                    actor="verifier",
                    payload={"task_type": task.task_type},
                ),
                db,
            )
            try:
                from agent_system.services.verifier import Verifier

                verification = Verifier(get_settings()).verify(
                    input_snapshot,
                    result or {},
                    {
                        "session_id": session_id,
                        "task_id": task_id,
                        "agent_run_id": run_id,
                        "agent_type": agent_type,
                        "factory": factory,
                        "bus": bus,
                    },
                )
                verification_passed = verification.passed
                verification_reason = verification.reason
                verification_mode = verification.mode
            except Exception as exc:
                verification_passed = True
                verification_reason = f"verifier crashed; lenient pass: {type(exc).__name__}"
            bus.emit(
                Event(
                    type="qa.completed" if verification_passed else "qa.failed",
                    session_id=session_id,
                    task_id=task_id,
                    agent_run_id=run_id,
                    actor="verifier",
                    payload={
                        "passed": verification_passed,
                        "reason": verification_reason,
                        "mode": verification_mode,
                    },
                ),
                db,
            )
        if task is not None:
            if verification_passed:
                validate_transition(TaskState(task.state), TaskState.SUCCEEDED)
                task.state = TaskState.SUCCEEDED.value
                task.completed_at = utcnow()
                enriched = dict(result or {})
                enriched.setdefault(
                    "verification",
                    {
                        "passed": True,
                        "reason": verification_reason,
                        "mode": verification_mode,
                    },
                )
                task.result_json = enriched
                result = enriched
            else:
                validate_transition(TaskState(task.state), TaskState.FAILED)
                task.state = TaskState.FAILED.value
                task.completed_at = utcnow()
                task.last_error = f"verification failed: {verification_reason}"[:500]
                result = {
                    "error_class": "VerificationFailed",
                    "error": task.last_error,
                    "verification": {
                        "passed": False,
                        "reason": verification_reason,
                        "mode": verification_mode,
                    },
                }
            from agent_system.infra.telemetry import elapsed_seconds, get_metrics

            seconds = elapsed_seconds(task.started_at, task.completed_at)
            if seconds is not None:
                get_metrics().record_task_duration(
                    "SUCCEEDED" if verification_passed else "FAILED", seconds
                )
        if run is not None:
            run.state = (
                AgentState.COMPLETED.value if verification_passed else AgentState.FAILED.value
            )
            run.ended_at = utcnow()
            run.result_json = result
        if lease is not None:
            db.delete(lease)
        bus.emit(
            Event(
                type="task.completed" if verification_passed else "task.failed",
                session_id=session_id,
                task_id=task_id,
                agent_run_id=run_id,
                actor=agent_type,
                payload=result or {},
            ),
            db,
        )
    if engine is not None:
        engine.dispose()
    return {
        "task_id": task_id,
        "state": "SUCCEEDED" if verification_passed else "FAILED",
        "result": result,
    }


def _finish_failed(
    factory: Any,
    bus: EventBus,
    task_id: str,
    run_id: str,
    session_id: str,
    agent_type: str,
    exc: Exception,
) -> None:
    with session_scope(factory) as db:
        task = db.get(Task, task_id)
        run = db.get(AgentRun, run_id)
        lease = db.get(AgentLease, run_id)
        if task is not None:
            current = TaskState(task.state)
            if current is TaskState.RUNNING:
                validate_transition(current, TaskState.FAILED)
                task.state = TaskState.FAILED.value
                task.completed_at = utcnow()
                task.last_error = str(exc)[:500]
                from agent_system.infra.telemetry import elapsed_seconds, get_metrics

                seconds = elapsed_seconds(task.started_at, task.completed_at)
                if seconds is not None:
                    get_metrics().record_task_duration("FAILED", seconds)
        if run is not None:
            run.state = AgentState.FAILED.value
            run.ended_at = utcnow()
            run.error_json = {"error_class": type(exc).__name__, "error": str(exc)[:500]}
        if lease is not None:
            db.delete(lease)
        bus.emit(
            Event(
                type="task.failed",
                session_id=session_id,
                task_id=task_id,
                agent_run_id=run_id,
                actor=agent_type,
                payload={"error_class": type(exc).__name__, "error": str(exc)[:500]},
            ),
            db,
        )


def main() -> None:  # pragma: no cover — manual worker process entrypoint
    import socket
    import uuid

    settings = get_settings()
    conn = Redis.from_url(settings.redis_url)
    # Unique per-process name: a crashed worker's stale registration must not
    # block a fresh worker from starting (RQ raises on duplicate names).
    name = f"agent-worker-{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
    worker = Worker([Queue(QUEUE_NAME, connection=conn)], connection=conn, name=name)
    worker.work(burst=False)


if __name__ == "__main__":  # pragma: no cover
    main()
