"""Cloud execution driver (Postgres-only, no Redis/RQ).

Heroku Basic runs a single web dyno: there is no worker dyno and no Redis,
so RQ-based background execution (``worker.py``) cannot run. This module
drives sessions **in-process** with the ``Orchestrator`` instead:

- ``ensure_session_tasks`` — a goal session with zero tasks gets planned;
  a goal that cannot be planned gets a single ``llm`` task carrying the
  goal, with the planner's inferred risk preserved (never silently lowered
  to LOW — the fallback must not reduce the security posture, INV-014).
- ``drive_session`` — installs the ReAct handler (like the RQ worker does),
  reclaims work orphaned by a crashed/restarted dyno (``recover_orphans``),
  then runs ready tasks to quiescence (bounded rounds).
- ``retry_task_queued`` — explicit operator retry ``FAILED -> QUEUED``
  (same transition the REST ``/tasks/{id}/retry`` endpoint allows).

Local behavior is unchanged: all three are only invoked when
``CLOUD_INLINE_RUN=true`` (Telegram cloud path).
"""

from __future__ import annotations

import re
from typing import Any

from agent_system.domain.tasks import TaskState, validate_transition
from agent_system.infra.db import session_scope
from agent_system.infra.models import Session, Task
from agent_system.services.planner import (
    HIGH_RISK_PATTERNS,
    PlannedTask,
    Planner,
    PlanningError,
    TaskPlan,
)

#: Hard stop on drive rounds (each round runs every currently-ready task).
MAX_DRIVE_ROUNDS = 25


def _fallback_plan(goal: str) -> TaskPlan:
    """Single generic ``llm`` task for an unplannable goal — risk preserved.

    INV-014: fallback execution never lowers the security posture. The
    planner's own high-risk classifier runs on the raw goal, so "rotate
    production credentials and deploy them" degrades to a HIGH-risk generic
    task, never a LOW one.
    """
    risk = "HIGH" if any(re.search(p, goal.lower()) for p in HIGH_RISK_PATTERNS) else "LOW"
    return TaskPlan(
        goal=goal,
        intent="generic",
        risk=risk,
        tasks=(
            PlannedTask(
                key="task_1",
                title=(goal[:80] or "cloud goal"),
                task_type="llm",
                agent_type="llm",
                input={"goal": goal},
                expected_outputs=("result",),
                risk=risk,
            ),
        ),
        notes=(
            "goal was not plannable by the deterministic planner; "
            f"single generic task at inferred risk {risk}",
        ),
    )


#: Public alias — the test suite pins the risk-preservation contract on this.
fallback_plan = _fallback_plan


def ensure_session_tasks(
    factory: Any, bus: Any, session_id: str, settings: Any = None
) -> list[str]:
    """Plan and create tasks for empty sessions; return the task ids present.

    Planning is the Planner's job (``services/planner.py``); the Supervisor
    validates and persists the resulting DAG. A goal that cannot be planned
    still produces one honest ``llm`` task so the session never stalls with
    zero work — the failure is recorded on the session, not hidden.
    """
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
    if settings is None:
        from agent_system.config import get_settings

        settings = get_settings()
    try:
        plan = Planner(settings).plan(goal)
    except PlanningError:
        # INV-014: fallback execution never lowers the security posture.
        plan = _fallback_plan(goal)
    from agent_system.services.tools.registry import build_registry

    task_ids = supervisor.apply_plan(
        factory,
        session_id,
        plan,
        available_capabilities=set(build_registry(settings).names()),
    )
    supervisor.plan(factory, session_id)
    return task_ids


def drive_session(
    factory: Any, bus: Any, session_id: str, max_rounds: int = MAX_DRIVE_ROUNDS
) -> dict[str, Any]:
    """Run a session's tasks in-process until quiescent (bounded rounds).

    Registers the ``llm`` ReAct handler explicitly: the in-process
    Orchestrator is strict by design (unknown types fail cleanly), so
    cloud tasks — always created as ``llm`` by ``ensure_session_tasks`` —
    need their handler composed here, mirroring the RQ worker's install.

    Restart recovery (INV-011): orphans from a crashed/restarted dyno
    (RUNNING with an expired lease) are reclaimed first, so accepted work is
    retried instead of stranded; tasks under a live lease are never touched.

    The summary reports ``unfinished`` > 0 whenever the bounded drive stopped
    with work it could not finish — round bound reached, or a task wedged
    under a live lease — so a safety-bound stop never masquerades as ordinary
    completion.
    """
    from agent_system.agents import react_agent
    from agent_system.services.orchestrator import Orchestrator

    try:
        react_agent.install()
    except Exception:
        pass
    orch = Orchestrator(bus)
    orch.register_handler("llm", react_agent.llm_react_handler)
    orch.recover_orphans(factory)
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
    unfinished = sum(
        1 for s in states if s not in (TaskState.SUCCEEDED.value, TaskState.FAILED.value)
    )
    failed_ids = [
        r.id for r in db_query_tasks(factory, session_id) if r.state == TaskState.FAILED.value
    ]
    return {
        "session_id": session_id,
        "succeeded": states.count(TaskState.SUCCEEDED.value),
        "failed": states.count(TaskState.FAILED.value),
        "total": len(states),
        "failed_task_ids": failed_ids,
        "unfinished": unfinished,
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
    "fallback_plan",
    "retry_task_queued",
]
