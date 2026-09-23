"""Bounded local task driver.

Explicit run/retry requests and recovery sweeps admit one driver per session.
Capacity is acquired before creating a thread; excess work stays QUEUED for
periodic recovery. The deadline stops new rounds, not blocked synchronous
handlers: hard cancellation requires a process execution boundary.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
import weakref
from typing import Any

from agent_system.domain.events import Event
from agent_system.domain.tasks import TaskState
from agent_system.infra.db import session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Task

#: Hard stop on drive rounds (each round runs every currently-ready task).
MAX_DRIVE_ROUNDS = 25

_kick_lock = threading.Lock()
_active: set[str] = set()
_shutdown = threading.Event()
_threads: weakref.WeakSet[threading.Thread] = weakref.WeakSet()
_semaphore: threading.BoundedSemaphore = threading.BoundedSemaphore(8)


def _default_concurrency() -> int:
    """Configured task concurrency cap, never below 1."""
    from agent_system.config import get_settings

    return max(1, int(get_settings().max_concurrent_tasks))


def reset_runner() -> None:
    """(Re)initialize runner state — called at API lifespan startup.

    Clears cancellation/shutdown flags and rebuilds the concurrency cap so a
    restarted process starts from a clean slate.
    """
    global _semaphore
    with _kick_lock:
        if any(thread.is_alive() for thread in _threads):
            raise RuntimeError("Cannot reset task runner while runner threads are still alive")
        _shutdown.clear()
        _active.clear()
        _semaphore = threading.BoundedSemaphore(_default_concurrency())


def shutdown_runner(timeout: float = 10.0) -> None:
    """Stop the runner: signal in-flight threads to exit and join them.

    Tasks interrupted mid-run are left RUNNING with an expiring lease; startup
    recovery (recover_orphans + sweep_backlog) reconciles them after restart.
    """
    _shutdown.set()
    deadline = time.monotonic() + timeout
    with _kick_lock:
        threads = list(_threads)
    for thread in threads:
        thread.join(timeout=max(0, deadline - time.monotonic()))
    with _kick_lock:
        if any(thread.is_alive() for thread in _threads):
            raise RuntimeError("Task runner shutdown timed out; runner threads are still alive")
        _active.clear()


def _build_orchestrator(bus: EventBus, settings: Any = None) -> Any:
    """Orchestrator with the ReAct handler installed (mirrors worker.py).

    Besides ``llm`` (what the planner always emits as ``agent_type``), the
    chat path creates ad-hoc ``general`` tasks — both resolve to the same
    ReAct loop, whose router self-heals an echo adapter offline, so chat
    works with zero keys configured. ``settings`` is threaded into the
    handler context (single-config execution); None preserves the legacy
    ambient read inside the handler.
    """
    from agent_system.agents import react_agent
    from agent_system.services.orchestrator import Orchestrator

    try:
        react_agent.install()
    except Exception:
        pass
    orch = Orchestrator(bus, settings=settings)
    orch.register_handler("llm", react_agent.llm_react_handler)
    # Ad-hoc chat tasks carry task_type "general" (agent_type defaults to it).
    orch.register_handler("general", react_agent.llm_react_handler)
    orch.register_handler("chat", react_agent.llm_react_handler)
    return orch


def _run_task_thread(
    factory: Any, bus: EventBus, task_id: str, session_id: str, settings: Any = None
) -> None:
    """Thread body: drive one task's session to quiescence (bounded rounds).

    Concurrency is capped by ``_semaphore``; every round honors the shutdown
    flag and a wall-clock deadline so a stuck session never spins forever.
    ``settings`` was resolved synchronously at kick time (never read lazily
    on the thread, where test monkeypatches no longer apply).
    """
    # Capacity is acquired before thread creation, so no waiting threads pile up.
    try:
        if _shutdown.is_set():
            return
        orch = _build_orchestrator(bus, settings=settings)
        if settings is None:
            from agent_system.config import get_settings

            settings = get_settings()
        deadline = time.monotonic() + max(1, int(settings.max_execution_time_seconds))
        try:
            for _ in range(max(1, MAX_DRIVE_ROUNDS)):
                if _shutdown.is_set() or time.monotonic() >= deadline:
                    break
                started = orch.run_ready_tasks(factory, session_id)
                # Quiescent when nothing (re)started this round — the kicked
                # task reached a terminal state or no successor is runnable.
                if not started:
                    break
        except Exception:
            logging.getLogger(__name__).exception("Task runner failed for %s", task_id)
            # Last-resort failure record: never leave a task stuck in RUNNING.
            try:
                with session_scope(factory) as db:
                    row = db.get(Task, task_id)
                    if row is not None and row.state == TaskState.RUNNING.value:
                        row.state = TaskState.FAILED.value
                        from agent_system.domain.events import utcnow

                        row.completed_at = utcnow()
                        row.last_error = "task runner crashed: see server logs"
                        bus.emit(
                            Event(
                                type="task.failed",
                                session_id=row.session_id,
                                task_id=task_id,
                                actor="task_runner",
                                payload={
                                    "error_class": "TaskRunnerCrash",
                                    "error": traceback.format_exc()[-500:],
                                },
                            ),
                            db,
                        )
            except Exception:
                pass
    finally:
        with _kick_lock:
            _semaphore.release()
            _active.discard(session_id)


def kick_task(factory: Any, bus: EventBus, task_id: str, settings: Any = None) -> bool:
    """Start executing one QUEUED task in a background thread.

    Returns True when a run was started (or is already active for this task).
    Only QUEUED tasks are runnable — PENDING needs an explicit queue first
    (``POST /tasks/{id}/run``), FAILED needs ``/retry`` — so stray kicks can
    never skip the lifecycle. Refuses to start after the runner was shut down.
    ``settings`` is resolved synchronously here (single-config execution):
    lazy reads on the worker thread would race test/request teardown.
    """
    if settings is None:
        from agent_system.config import get_settings

        settings = get_settings()
    if _shutdown.is_set():
        return False
    with session_scope(factory) as db:
        row = db.get(Task, task_id)
        if row is None:
            return False
        if row.state != TaskState.QUEUED.value:
            return False
        session_id = row.session_id
    with _kick_lock:
        if _shutdown.is_set():
            return False
        if session_id in _active:
            return True
        if not _semaphore.acquire(blocking=False):
            return False  # leave durable QUEUED work for the periodic sweep
        _active.add(session_id)
        thread = threading.Thread(
            target=_run_task_thread,
            args=(factory, bus, task_id, session_id, settings),
            name=f"task-runner-{task_id[-8:]}",
            daemon=True,
        )
        _threads.add(thread)
        try:
            thread.start()
        except BaseException:
            _threads.discard(thread)
            _active.discard(session_id)
            _semaphore.release()
            raise
    return True


def sweep_backlog(factory: Any, bus: EventBus, settings: Any = None) -> list[str]:
    """Re-kick tasks left QUEUED by a previous process (startup recovery).

    Only runs at API startup (lifespan), never per-request, so it cannot race
    the contract tests' manual lifecycle walks.
    """
    if settings is None:
        from agent_system.config import get_settings

        settings = get_settings()
    kicked: list[str] = []
    with session_scope(factory) as db:
        rows = db.query(Task).filter_by(state=TaskState.QUEUED.value).all()
        ids = [r.id for r in rows]
    for task_id in ids:
        try:
            if kick_task(factory, bus, task_id, settings=settings):
                kicked.append(task_id)
        except Exception:
            continue
    return kicked
