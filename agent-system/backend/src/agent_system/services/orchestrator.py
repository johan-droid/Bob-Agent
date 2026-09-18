"""Supervisor / Orchestrator (v3.1 §9, §7, §8, §11).

- Supervisor: goal -> task DAG with cycle detection and limits.
- Orchestrator: runs tasks via agent handlers, tracks AgentRuns with
  leases/heartbeats, recovers orphans after crashes, supports cancellation.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

from agent_system.domain import ids
from agent_system.domain.events import Event, utcnow
from agent_system.domain.lifecycles import AgentState
from agent_system.domain.tasks import TERMINAL_STATES, TaskState, validate_transition
from agent_system.infra.db import session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import AgentLease, AgentRun, Session, Task

AgentHandler = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
"""handler(task_input, context) -> result dict. Raises on failure."""

LEASE_TTL_SECONDS = 30
MAX_TASKS_PER_SESSION = 100
MAX_AGENT_SPAWN_PER_TASK = 5


def _concurrency_limit() -> int:
    """Configured task concurrency cap, never below 1.

    Read per claim (not cached at import) so tests and settings changes take
    effect without process restarts.
    """
    try:
        from agent_system.config import get_settings

        return max(1, int(get_settings().max_concurrent_tasks))
    except Exception:
        return 1


#: States that hold a live execution slot (RUNNING, plus REVIEW which still
#: carries the handler result and heartbeat lease until _finish releases it).
_LIVE_SLOT_STATES = (TaskState.RUNNING.value, TaskState.REVIEW.value)


def claim_queued_task(db: Any, task_id: str) -> bool:
    """Atomically claim one QUEUED task for execution iff under the cap.

    Resource enforcement (INV-010): the concurrency guard and the
    ``QUEUED -> RUNNING`` transition are a single conditional UPDATE —
    ``UPDATE task SET state='RUNNING' ... WHERE id=:id AND state='QUEUED'
    AND (SELECT COUNT(*) FROM task WHERE state IN ('RUNNING','REVIEW')) < :limit``
    — so racing drivers cannot exceed the limit via check-then-act: the loser's
    WHERE fails and its task stays QUEUED for a later round/sweep. This is the
    one claim seam shared by the in-process orchestrator and the RQ worker.

    ``ponytail:`` SQLite serializes writers so the guard is exact; on Postgres
    concurrent claims of DIFFERENT tasks can overshoot by the width of the
    statement race window (self-correcting on the next sweep). Upgrade to
    advisory locks only if multi-writer overshoot ever measurably matters.
    """
    running_sq = (
        select(func.count())
        .select_from(Task)
        .where(Task.state.in_(_LIVE_SLOT_STATES))
        .scalar_subquery()
    )
    claimed: int = (
        db.query(Task)
        .filter(
            Task.id == task_id,
            Task.state == TaskState.QUEUED.value,
            running_sq < _concurrency_limit(),
        )
        .update(
            {
                "state": TaskState.RUNNING.value,
                "started_at": utcnow(),
                "attempt": Task.attempt + 1,
            },
            synchronize_session=False,
        )
    )
    return claimed > 0


class CycleError(ValueError):
    pass


class Supervisor:
    """Validates and persists planned work; schedules ready tasks.

    The Supervisor does *not* decide what work exists — the Planner does that
    (``services/planner.py``). The Supervisor owns: DAG validation (cycles,
    unknown dependencies, hard limits), task persistence, lifecycle accounting,
    and scheduling of ready work.
    """

    def __init__(self, event_bus: EventBus) -> None:
        self._bus = event_bus

    # -- planning seam ------------------------------------------------------

    def validate_plan(self, plan: Any, available_capabilities: Any = None) -> list[str]:
        """Validate a planner's DAG; returns warnings, raises on violations.

        Violations fail closed: a dependency cycle, an unknown dependency, an
        over-limit plan, or a required capability that does not exist in the
        capability library is a planning bug, not something to paper over at
        execution time.
        """
        keys = [task.key for task in plan.tasks]
        if len(keys) != len(set(keys)):
            raise ValueError("plan has duplicate task keys")
        if len(plan.tasks) > MAX_TASKS_PER_SESSION:
            raise ValueError(f"plan exceeds the per-session task limit ({MAX_TASKS_PER_SESSION})")
        graph = {task.key: list(task.depends_on) for task in plan.tasks}
        for key, deps in graph.items():
            for dep in deps:
                if dep not in graph:
                    raise ValueError(f"plan task '{key}' depends on unknown task '{dep}'")
        self._assert_acyclic_keys(graph)
        warnings: list[str] = []
        if available_capabilities is not None:
            known = set(available_capabilities)
            for task in plan.tasks:
                missing = [c for c in task.required_capabilities if c not in known]
                if missing:
                    raise ValueError(
                        f"plan task '{task.key}' requires unavailable capabilities: "
                        f"{', '.join(missing)}"
                    )
        if plan.risk in {"HIGH", "CRITICAL"}:
            warnings.append(
                f"plan classified {plan.risk} risk; expect approval requests during execution"
            )
        return warnings

    def apply_plan(
        self, factory: Any, session_id: str, plan: Any, **validate_kwargs: Any
    ) -> list[str]:
        """Persist a validated plan as tasks, mapping planned keys to task ids."""
        self.validate_plan(plan, **validate_kwargs)
        created: dict[str, str] = {}
        ordered = self._topological_order(plan)
        for task in ordered:
            task_id = self.add_task(
                factory,
                session_id,
                task_type=task.task_type,
                title=task.title,
                input_json=dict(task.input),
                depends_on=[created[dep] for dep in task.depends_on if dep in created],
                agent_type=task.agent_type,
            )
            created[task.key] = task_id
        return [created[task.key] for task in ordered]

    def create_planned_session(
        self, factory: Any, goal: str, settings: Any = None, session_id: str | None = None
    ) -> tuple[str, Any, list[str]]:
        """Plan a goal and persist it: session -> planned DAG -> queued tasks.

        When ``session_id`` is given the plan is applied to that existing
        session; otherwise a new session is created for the goal. Applying to
        the caller's session matters: planning must never silently target a
        different session than the one the request named.
        """
        from agent_system.services.planner import Planner
        from agent_system.services.tools.registry import build_registry

        # Planner consults the Model Layer first (LLM DAG) with deterministic
        # fallback; the bus + factory let the model call be recorded with the
        # target session id for cost accounting.
        plan = Planner(settings).plan(goal, factory=factory, bus=self._bus, session_id=session_id)
        target_session = session_id or self.create_session(factory, goal)
        try:
            known = set(build_registry(settings).names()) if settings is not None else None
            task_ids = self.apply_plan(factory, target_session, plan, available_capabilities=known)
        except ValueError:
            self._fail_session(factory, target_session, plan)
            raise
        self.plan(factory, target_session)
        return target_session, plan, task_ids

    def _fail_session(self, factory: Any, session_id: str, plan: Any) -> None:
        """Record the planning failure on the session rather than leaving it ACTIVE."""
        from agent_system.infra.models import Session

        with session_scope(factory) as db:
            row = db.get(Session, session_id)
            if row is not None:
                row.status = "PLANNING_FAILED"
            self._bus.emit(
                Event(
                    type="session.completed",
                    session_id=session_id,
                    actor="supervisor",
                    payload={"outcome": "PLANNING_FAILED", "plan": plan.to_json()},
                ),
                db,
            )

    def _topological_order(self, plan: Any) -> list[Any]:
        by_key = {task.key: task for task in plan.tasks}
        ordered: list[Any] = []
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visited:
                return
            for dep in by_key[key].depends_on:
                visit(dep)
            visited.add(key)
            ordered.append(by_key[key])

        for key in by_key:
            visit(key)
        return ordered

    @staticmethod
    def _assert_acyclic_keys(graph: dict[str, list[str]]) -> None:
        visiting: set[str] = set()
        done: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise CycleError(f"dependency cycle detected at {node}")
            if node in done:
                return
            visiting.add(node)
            for dep in graph.get(node, []):
                visit(dep)
            visiting.discard(node)
            done.add(node)

        for node in graph:
            visit(node)

    def create_session(self, factory: Any, goal: str, owner_user_id: str | None = None) -> str:
        session_id = ids.new_session_id()
        with session_scope(factory) as db:
            db.add(
                Session(
                    id=session_id,
                    goal=goal,
                    status="ACTIVE",
                    owner_user_id=owner_user_id,
                )
            )
            self._bus.emit(
                Event(
                    type="session.created",
                    session_id=session_id,
                    actor="supervisor",
                    payload={"goal": goal[:200]},
                ),
                db,
            )
        return session_id

    def session_status(self, factory: Any) -> dict[str, int]:
        """Aggregate task-session counts for the /status UX (Telegram + CLI)."""
        from agent_system.domain.tasks import TaskState

        with session_scope(factory) as db:
            total_sessions = db.query(Session).count()
            total = db.query(Task).count()
            running = db.query(Task).filter(Task.state == TaskState.RUNNING.value).count()
            queued = db.query(Task).filter(Task.state == TaskState.QUEUED.value).count()
            waiting_approval = (
                db.query(Task).filter(Task.state == TaskState.BLOCKED_APPROVAL.value).count()
            )
            complete = db.query(Task).filter(Task.state == TaskState.SUCCEEDED.value).count()
            failed = db.query(Task).filter(Task.state == TaskState.FAILED.value).count()
        return {
            "sessions": total_sessions,
            "total": total,
            "running": running,
            "queued": queued,
            "waiting_approval": waiting_approval,
            "complete": complete,
            "failed": failed,
        }

    def add_task(
        self,
        factory: Any,
        session_id: str,
        task_type: str,
        title: str,
        input_json: dict[str, Any] | None = None,
        depends_on: list[str] | None = None,
        agent_type: str | None = None,
    ) -> str:
        depends_on = depends_on or []
        with session_scope(factory) as db:
            count = db.query(Task).filter_by(session_id=session_id).count()
            if count >= MAX_TASKS_PER_SESSION:
                raise ValueError(f"session task limit ({MAX_TASKS_PER_SESSION}) exceeded")
            for dep in depends_on:
                if db.get(Task, dep) is None:
                    raise ValueError(f"unknown dependency: {dep}")
            task_id = ids.new_task_id()
            db.add(
                Task(
                    id=task_id,
                    session_id=session_id,
                    task_type=task_type,
                    title=title,
                    input_json=input_json or {},
                    depends_on_json=depends_on,
                    agent_type=agent_type or task_type,
                    state=TaskState.PENDING.value,
                )
            )
            self._bus.emit(
                Event(
                    type="task.created", session_id=session_id, task_id=task_id, actor="supervisor"
                ),
                db,
            )
        return task_id

    def plan(self, factory: Any, session_id: str) -> list[str]:
        """Validate the DAG (cycles, unknown deps) and queue ready tasks.

        Returns ids of tasks transitioned PENDING -> QUEUED.
        """
        queued: list[str] = []
        with session_scope(factory) as db:
            tasks = db.query(Task).filter_by(session_id=session_id).all()
            by_id = {t.id: t for t in tasks}
            self._assert_acyclic(tasks)
            for task in tasks:
                if task.state != TaskState.PENDING.value:
                    continue
                deps = task.depends_on_json or []
                if all(by_id[d].state == TaskState.SUCCEEDED.value for d in deps if d in by_id):
                    validate_transition(TaskState.PENDING, TaskState.QUEUED)
                    task.state = TaskState.QUEUED.value
                    queued.append(task.id)
                    self._bus.emit(
                        Event(
                            type="task.queued",
                            session_id=session_id,
                            task_id=task.id,
                            actor="supervisor",
                        ),
                        db,
                    )
        return queued

    @staticmethod
    def _assert_acyclic(tasks: list[Task]) -> None:
        graph: dict[str, list[str]] = {t.id: list(t.depends_on_json or []) for t in tasks}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise CycleError(f"dependency cycle detected at {node}")
            if node in visited:
                return
            visiting.add(node)
            for dep in graph.get(node, []):
                visit(dep)
            visiting.remove(node)
            visited.add(node)

        for node in graph:
            visit(node)


class Orchestrator:
    """Executes queued tasks with isolated agent runs + lease tracking.

    Successful handler results pass through the Verifier while the task is in
    REVIEW (``RUNNING -> REVIEW -> SUCCEEDED/FAILED`` with ``qa.*`` events),
    so the VERIFIER box in the architecture is an enforced gate rather than a
    sidecar. The default verifier is lenient: tasks without verifiable
    artifacts pass with a recorded reason.
    """

    def __init__(
        self,
        event_bus: EventBus,
        heartbeat_interval: float = 5.0,
        verifier: Any = None,
        settings: Any = None,
    ) -> None:
        self._bus = event_bus
        self._handlers: dict[str, AgentHandler] = {}
        self._lock = threading.Lock()
        self._cancel_requested: set[str] = set()
        self._heartbeat_interval = heartbeat_interval
        self._verifier = verifier
        self._settings = settings

    @property
    def verifier(self) -> Any:
        """Lazily constructed default verifier (lenient, never breaks runs)."""
        if self._verifier is None:
            try:
                from agent_system.services.verifier import Verifier

                settings = self._settings
                if settings is None:
                    try:
                        from agent_system.config import get_settings

                        settings = get_settings()
                    except Exception:
                        settings = None
                self._verifier = Verifier(settings)
            except Exception:
                self._verifier = None
        return self._verifier

    # -- registration -------------------------------------------------------

    def register_handler(self, agent_type: str, handler: AgentHandler) -> None:
        with self._lock:
            self._handlers[agent_type] = handler

    # -- execution ----------------------------------------------------------

    def run_ready_tasks(self, factory: Any, session_id: str) -> list[str]:
        """Queue newly-ready PENDING tasks, then run every queued task whose
        dependencies are all SUCCEEDED. Safe to call repeatedly after each
        completion — drives the DAG forward autonomously."""
        started: list[str] = []
        with session_scope(factory) as db:
            all_tasks = db.query(Task).filter_by(session_id=session_id).all()
            by_id = {t.id: t for t in all_tasks}
            # Newly-ready PENDING tasks -> QUEUED (their deps completed since
            # the last plan/run pass).
            for task in all_tasks:
                if task.state != TaskState.PENDING.value:
                    continue
                deps = task.depends_on_json or []
                if all(by_id[d].state == TaskState.SUCCEEDED.value for d in deps if d in by_id):
                    validate_transition(TaskState.PENDING, TaskState.QUEUED)
                    task.state = TaskState.QUEUED.value
                    self._bus.emit(
                        Event(
                            type="task.queued",
                            session_id=session_id,
                            task_id=task.id,
                            actor="orchestrator",
                            payload={"auto": True},
                        ),
                        db,
                    )
        with session_scope(factory) as db:
            tasks = (
                db.query(Task).filter_by(session_id=session_id, state=TaskState.QUEUED.value).all()
            )
            by_id = {t.id: t for t in db.query(Task).filter_by(session_id=session_id).all()}
            for task in tasks:
                deps = task.depends_on_json or []
                if all(by_id[d].state == TaskState.SUCCEEDED.value for d in deps if d in by_id):
                    if self._run_task(factory, task.id):
                        started.append(task.id)
        return started

    def _run_task(self, factory: Any, task_id: str) -> bool:
        """Execute one task synchronously with a full agent run lifecycle."""
        with session_scope(factory) as db:
            # Guarded claim FIRST — it must be the session's first statement so
            # its snapshot (and the cap subquery) is fresh. Exactly one claimant
            # moves QUEUED -> RUNNING, under the concurrency cap (INV-010).
            # attempt counts "times execution started" — incremented on every
            # entry into RUNNING (worker, API transition, and this in-process
            # path). Requeue/recovery paths must NOT increment.
            if not claim_queued_task(db, task_id):
                return False
            task = db.get(Task, task_id)
            if task is None:
                return False
            run_id = ids.new_agent_run_id()
            agent_type = task.agent_type or task.task_type
            db.add(
                AgentRun(
                    id=run_id,
                    task_id=task_id,
                    agent_type=agent_type,
                    state=AgentState.RUNNING.value,
                    worker_id="inproc",
                )
            )
            db.add(
                AgentLease(
                    agent_run_id=run_id,
                    worker_id="inproc",
                    state=AgentState.RUNNING.value,
                    heartbeat_at=utcnow(),
                    lease_expires_at=utcnow() + timedelta(seconds=LEASE_TTL_SECONDS),
                )
            )
            self._bus.emit(
                Event(
                    type="task.started",
                    session_id=task.session_id,
                    task_id=task_id,
                    agent_run_id=run_id,
                    actor=agent_type,
                ),
                db,
            )
            self._bus.emit(
                Event(
                    type="agent.started",
                    session_id=task.session_id,
                    task_id=task_id,
                    agent_run_id=run_id,
                    actor=agent_type,
                ),
                db,
            )
            session_id = task.session_id
            agent_type_snapshot = agent_type
            input_snapshot = dict(task.input_json or {})

        if task_id in self._cancel_requested:
            self._finish(
                factory, task_id, run_id, session_id, agent_type_snapshot, TaskState.CANCELLED, {}
            )
            return True

        stop = threading.Event()
        hb = threading.Thread(
            target=self._heartbeat_loop,
            args=(factory, run_id, stop),
            daemon=True,
            name=f"lease-hb-{task_id[-8:]}",
        )
        hb.start()
        try:
            handler = self._handlers.get(agent_type_snapshot)
            if handler is None:
                # Strict by design (pinned by
                # tests/integration/test_orchestrator.py): unregistered types
                # fail cleanly with LookupError. The goal-aware LLM fallback
                # lives in the RQ worker (registry) and in cloud drive
                # (services/cloud.py registers "llm" explicitly) — never as an
                # implicit orchestrator default.
                from agent_system.agents.react_agent import fallback_handler

                handler = fallback_handler(input_snapshot)
            if handler is None:
                raise LookupError(f"no handler registered for agent type '{agent_type_snapshot}'")
            context = {
                "session_id": session_id,
                "task_id": task_id,
                "agent_run_id": run_id,
                "agent_type": agent_type_snapshot,
                "factory": factory,
                "bus": self._bus,
            }
            result = handler(input_snapshot, context)
            self._verify_and_finish(
                factory,
                task_id,
                run_id,
                session_id,
                agent_type_snapshot,
                input_snapshot,
                result or {},
            )
            return True
        except Exception as exc:
            self._finish(
                factory,
                task_id,
                run_id,
                session_id,
                agent_type_snapshot,
                TaskState.FAILED,
                {"error_class": type(exc).__name__, "error": str(exc)[:500]},
            )
            return False
        finally:
            stop.set()
            hb.join(timeout=2)

    def _verify_and_finish(
        self,
        factory: Any,
        task_id: str,
        run_id: str,
        session_id: str,
        agent_type: str,
        task_input: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """RUNNING -> REVIEW (verify) -> SUCCEEDED/FAILED.

        The REVIEW state is now the enforced verifier gate: the task enters
        REVIEW, ``qa.started`` is emitted, the Verifier runs, then
        ``qa.completed``/``qa.failed`` is emitted and the task finishes. The
        verifier never wedges execution — a verifier crash degrades to the
        verifier's own lenient/strict policy.
        """
        with session_scope(factory) as db:
            task = db.get(Task, task_id)
            if task is not None and TaskState(task.state) == TaskState.RUNNING:
                validate_transition(TaskState.RUNNING, TaskState.REVIEW)
                task.state = TaskState.REVIEW.value
                self._bus.emit(
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
            else:
                # Not in RUNNING (e.g. cancelled mid-handler): skip verification
                # and let _finish reconcile the terminal/cancelled state.
                self._finish(
                    factory, task_id, run_id, session_id, agent_type, TaskState.SUCCEEDED, result
                )
                return
        verifier = self.verifier
        try:
            context = {
                "session_id": session_id,
                "task_id": task_id,
                "agent_run_id": run_id,
                "agent_type": agent_type,
                "factory": factory,
                "bus": self._bus,
            }
            verification = verifier.verify(task_input, result, context) if verifier else None
        except Exception as exc:  # belt-and-braces; Verifier.verify never raises
            from agent_system.services.verifier import VerificationResult

            verification = VerificationResult(
                True, f"verifier crashed; lenient pass: {type(exc).__name__}"
            )
        if verification is None or verification.passed:
            reason = verification.reason if verification else "no verifier"
            mode = verification.mode if verification else "off"
            enriched = dict(result)
            enriched.setdefault("verification", {"passed": True, "reason": reason, "mode": mode})
            with session_scope(factory) as db:
                self._bus.emit(
                    Event(
                        type="qa.completed",
                        session_id=session_id,
                        task_id=task_id,
                        agent_run_id=run_id,
                        actor="verifier",
                        payload={"passed": True, "reason": reason, "mode": mode},
                    ),
                    db,
                )
            self._finish(
                factory, task_id, run_id, session_id, agent_type, TaskState.SUCCEEDED, enriched
            )
            return
        payload = {
            "error_class": "VerificationFailed",
            "error": f"verification failed: {verification.reason}"[:500],
            "verification": {
                "passed": False,
                "reason": verification.reason,
                "mode": verification.mode,
            },
        }
        with session_scope(factory) as db:
            self._bus.emit(
                Event(
                    type="qa.failed",
                    session_id=session_id,
                    task_id=task_id,
                    agent_run_id=run_id,
                    actor="verifier",
                    payload={
                        "passed": False,
                        "reason": verification.reason,
                        "mode": verification.mode,
                    },
                ),
                db,
            )
        self._finish(factory, task_id, run_id, session_id, agent_type, TaskState.FAILED, payload)

    def _heartbeat_loop(self, factory: Any, run_id: str, stop: threading.Event) -> None:
        """Refresh the lease heartbeat while the run is executing (mirrors worker.py)."""
        while not stop.wait(self._heartbeat_interval):
            try:
                with session_scope(factory) as db:
                    lease = db.get(AgentLease, run_id)
                    if lease is None:
                        return
                    lease.heartbeat_at = utcnow()
                    lease.lease_expires_at = utcnow() + timedelta(seconds=LEASE_TTL_SECONDS)
            except Exception:
                return  # DB gone — recover_orphans handles orphan detection

    def _finish(
        self,
        factory: Any,
        task_id: str,
        run_id: str,
        session_id: str,
        agent_type: str,
        state: TaskState,
        result: dict[str, Any],
    ) -> None:
        with session_scope(factory) as db:
            task = db.get(Task, task_id)
            run = db.get(AgentRun, run_id)
            lease = db.get(AgentLease, run_id)
            if task is not None:
                current = TaskState(task.state)
                if current in TERMINAL_STATES:
                    # Idempotent: the task already ended (e.g. cancelled while
                    # this handler was finishing). Reconcile the run/lease and
                    # surface the already-recorded terminal state — never raise
                    # a stale-transition error from a completion race.
                    effective = current
                else:
                    validate_transition(current, state)
                    effective = state
                task.state = effective.value
                task.completed_at = utcnow()
                if effective == TaskState.FAILED:
                    task.last_error = str(result.get("error", ""))[:500]
                if effective == TaskState.SUCCEEDED:
                    task.result_json = result
            else:
                effective = state
            if run is not None:
                run.state = (
                    AgentState.COMPLETED.value
                    if effective == TaskState.SUCCEEDED
                    else AgentState.FAILED.value
                    if effective == TaskState.FAILED
                    else AgentState.TERMINATED.value
                )
                run.ended_at = utcnow()
                run.result_json = result
            if lease is not None:
                db.delete(lease)
            event_type = {
                TaskState.SUCCEEDED: "task.completed",
                TaskState.FAILED: "task.failed",
                TaskState.CANCELLED: "task.cancelled",
            }[effective]
            self._bus.emit(
                Event(
                    type=event_type,
                    session_id=session_id,
                    task_id=task_id,
                    agent_run_id=run_id,
                    actor=agent_type,
                    payload=result,
                ),
                db,
            )
            self._bus.emit(
                Event(
                    type="agent.completed"
                    if effective == TaskState.SUCCEEDED
                    else ("agent.failed" if effective == TaskState.FAILED else "agent.terminated"),
                    session_id=session_id,
                    task_id=task_id,
                    agent_run_id=run_id,
                    actor=agent_type,
                ),
                db,
            )
        self._cancel_requested.discard(task_id)

    # -- cancellation -------------------------------------------------------

    def cancel_task(self, factory: Any, task_id: str) -> bool:
        """Cancel a queued task now, or request cancellation of a running one."""
        with session_scope(factory) as db:
            task = db.get(Task, task_id)
            if task is None:
                return False
            state = TaskState(task.state)
            if state == TaskState.QUEUED:
                validate_transition(state, TaskState.CANCELLED)
                task.state = TaskState.CANCELLED.value
                self._bus.emit(Event(type="task.cancelled", task_id=task_id, actor="user"), db)
                return True
            if state == TaskState.RUNNING:
                self._cancel_requested.add(task_id)
                return True
            return False

    # -- crash recovery -------------------------------------------------------

    def recover_orphans(self, factory: Any) -> list[str]:
        """Find RUNNING tasks whose lease expired and mark them for retry/fail.

        A task whose attempt < max_retries goes back to QUEUED (RECOVERING
        transition recorded); otherwise it fails. Emits recovery events.
        """
        recovered: list[str] = []
        now = utcnow()
        with session_scope(factory) as db:
            stale_leases = db.query(AgentLease).filter(AgentLease.lease_expires_at < now).all()
            for lease in stale_leases:
                run = db.get(AgentRun, lease.agent_run_id)
                if run is None:
                    db.delete(lease)
                    continue
                task = db.get(Task, run.task_id)
                db.delete(lease)
                if task is None or task.state != TaskState.RUNNING.value:
                    continue
                self._bus.emit(
                    Event(
                        type="recovery.started",
                        task_id=task.id,
                        actor="orchestrator",
                        payload={"reason": "stale_lease", "agent_run_id": run.id},
                    ),
                    db,
                )
                if task.attempt < 3:
                    validate_transition(TaskState.RUNNING, TaskState.RECOVERING)
                    task.state = TaskState.RECOVERING.value
                    validate_transition(TaskState.RECOVERING, TaskState.QUEUED)
                    task.state = TaskState.QUEUED.value
                    # attempt counts "times started" — incremented at the
                    # RUNNING transition (worker/runner), not here, else a
                    # crashed run would double-count.
                    self._bus.emit(
                        Event(
                            type="recovery.completed",
                            task_id=task.id,
                            actor="orchestrator",
                            payload={"action": "requeued", "attempt": task.attempt},
                        ),
                        db,
                    )
                else:
                    validate_transition(TaskState.RUNNING, TaskState.FAILED)
                    task.state = TaskState.FAILED.value
                    task.last_error = "lease expired; retries exhausted"
                    self._bus.emit(
                        Event(
                            type="recovery.failed",
                            task_id=task.id,
                            actor="orchestrator",
                            payload={"action": "failed_after_retries"},
                        ),
                        db,
                    )
                run.state = AgentState.FAILED.value
                run.ended_at = now
                recovered.append(task.id)
        return recovered
