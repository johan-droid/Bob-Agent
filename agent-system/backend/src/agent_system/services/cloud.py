"""Cloud execution driver (Postgres-only, no Redis/RQ).

Heroku Basic runs a single web dyno: there is no worker dyno and no Redis,
so RQ-based background execution (``worker.py``) cannot run. This module
drives sessions **in-process** with the ``Orchestrator`` instead:

- ``ensure_session_tasks`` — a goal session with zero tasks gets a single
  ``llm`` task carrying ``{"goal": ...}`` (planned QUEUED).
- ``drive_session`` — installs the ReAct handler (like the RQ worker does),
  then runs ready tasks to quiescence (bounded rounds).
- ``retry_task_queued`` — explicit operator retry ``FAILED -> QUEUED``
  (same transition the REST ``/tasks/{id}/retry`` endpoint allows).

Local behavior is unchanged: all three are only invoked when
``CLOUD_INLINE_RUN=true`` (Telegram cloud path).
"""

from __future__ import annotations

from typing import Any

from agent_system.domain.tasks import TaskState, validate_transition
from agent_system.infra.db import session_scope
from agent_system.infra.models import Session, Task

#: Hard stop on drive rounds (each round runs every currently-ready task).
MAX_DRIVE_ROUNDS = 25


def ensure_session_tasks(factory: Any, bus: Any, session_id: str) -> list[str]:
    """Create a single goal task for empty sessions; return task ids present."""
    from agent_system.services.orchestrator import Supervisor

    supervisor = Supervisor(bus)
    with session_scope(factory) as db:
        session = db.get(Session, session_id)
        if session is None:
            raise LookupError(f"session {session_id} not found")
        existing = db.query(Task).filter_by(session_id=session_id).all()
        if existing:
            return [t.id for t in existing]
        goal = session.goal or ""
    task_id = supervisor.add_task(
        factory,
        session_id,
        task_type="llm",
        title=(goal[:80] or "cloud goal"),
        input_json={"goal": goal},
        agent_type="llm",
    )
    supervisor.plan(factory, session_id)
    return [task_id]


def drive_session(
    factory: Any, bus: Any, session_id: str, max_rounds: int = MAX_DRIVE_ROUNDS
) -> dict[str, Any]:
    """Run a session's tasks in-process until quiescent (bounded rounds).

    Registers the ``llm`` ReAct handler explicitly: the in-process
    Orchestrator is strict by design (unknown types fail cleanly), so
    cloud tasks — always created as ``llm`` by ``ensure_session_tasks`` —
    need their handler composed here, mirroring the RQ worker's install.
    """
    from agent_system.agents import react_agent
    from agent_system.services.orchestrator import Orchestrator

    try:
        react_agent.install()
    except Exception:
        pass
    orch = Orchestrator(bus)
    orch.register_handler("llm", react_agent.llm_react_handler)
    ensure_session_tasks(factory, bus, session_id)
    for _ in range(max(1, max_rounds)):
        started = orch.run_ready_tasks(factory, session_id)
        with session_scope(factory) as db:
            queued = (
                db.query(Task)
                .filter_by(session_id=session_id, state=TaskState.QUEUED.value)
                .count()
            )
            rows = db.query(Task).filter_by(session_id=session_id).all()
            states = [r.state for r in rows]
        if not started and queued == 0:
            break
    succeeded = sum(1 for s in states if s == TaskState.SUCCEEDED.value)
    failed = sum(1 for s in states if s == TaskState.FAILED.value)
    failed_ids = [
        r.id for r in db_query_tasks(factory, session_id) if r.state == TaskState.FAILED.value
    ]
    return {
        "session_id": session_id,
        "succeeded": succeeded,
        "failed": failed,
        "total": len(states),
        "failed_task_ids": failed_ids,
    }


def db_query_tasks(factory: Any, session_id: str) -> list[Any]:
    """List task rows for a session (fresh read, for summaries)."""
    with session_scope(factory) as db:
        return db.query(Task).filter_by(session_id=session_id).all()


def retry_task_queued(factory: Any, bus: Any, task_id: str) -> str:
    """Explicit retry ``FAILED -> QUEUED`` + ``task.queued`` event."""
    from agent_system.domain.events import Event

    with session_scope(factory) as db:
        row = db.get(Task, task_id)
        if row is None:
            raise LookupError(f"task {task_id} not found")
        current = TaskState(row.state)
        # Raises InvalidTransitionError for non-retryable states (same rule
        # as the REST retry endpoint) — never coerced.
        validate_transition(current, TaskState.QUEUED)
        row.state = TaskState.QUEUED.value
        bus.emit(
            Event(
                type="task.queued",
                session_id=row.session_id,
                task_id=row.id,
                actor="user",
                payload={"retry": True, "attempt": row.attempt},
            ),
            db,
        )
        return str(row.session_id)


__all__ = [
    "MAX_DRIVE_ROUNDS",
    "db_query_tasks",
    "drive_session",
    "ensure_session_tasks",
    "retry_task_queued",
]
