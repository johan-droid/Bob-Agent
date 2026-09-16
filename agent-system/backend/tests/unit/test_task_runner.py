"""Tests for the in-process task runner lifecycle guards."""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest

from agent_system.services import task_runner


@pytest.fixture()
def _clean_runner() -> Iterator[None]:
    task_runner.reset_runner()
    yield
    task_runner.shutdown_runner(timeout=5)
    task_runner.reset_runner()


@pytest.mark.parametrize("shutting_down", [False, True])
def test_reset_runner_refuses_while_threads_alive(_clean_runner: None, shutting_down: bool) -> None:
    started = threading.Event()
    release = threading.Event()

    def _spinner() -> None:
        started.set()
        release.wait(5)

    thread = threading.Thread(target=_spinner, daemon=True)
    with task_runner._kick_lock:
        task_runner._threads.add(thread)
    thread.start()
    try:
        assert started.wait(5)
        if shutting_down:
            task_runner._shutdown.set()
        task_runner._active.add("test-task")
        semaphore = task_runner._semaphore
        with pytest.raises(RuntimeError, match="still alive"):
            task_runner.reset_runner()
        assert task_runner._shutdown.is_set() is shutting_down
        assert task_runner._active == {"test-task"}
        assert task_runner._semaphore is semaphore
    finally:
        release.set()
        thread.join(5)
    task_runner.reset_runner()
    assert not task_runner._shutdown.is_set()


def test_shutdown_preserves_live_thread_tracking(_clean_runner: None) -> None:
    release = threading.Event()
    thread = threading.Thread(target=release.wait, daemon=True)
    with task_runner._kick_lock:
        task_runner._threads.add(thread)
        task_runner._active.add("session")
        thread.start()
    try:
        with pytest.raises(RuntimeError, match="shutdown timed out"):
            task_runner.shutdown_runner(timeout=0.01)
        assert task_runner._shutdown.is_set()
        assert "session" in task_runner._active
        with pytest.raises(RuntimeError, match="still alive"):
            task_runner.reset_runner()
    finally:
        release.set()
        thread.join(5)


def test_capacity_is_acquired_before_thread_creation(
    _clean_runner: None, monkeypatch: pytest.MonkeyPatch, db
) -> None:
    from agent_system.infra.db import make_session_factory
    from agent_system.infra.event_bus import EventBus
    from agent_system.services.orchestrator import Supervisor

    factory = make_session_factory(db.bind)
    bus = EventBus()
    supervisor = Supervisor(bus)
    tasks = []
    for index in range(2):
        session_id = supervisor.create_session(factory, f"session {index}")
        tasks.append(supervisor.add_task(factory, session_id, "code", "test"))
        supervisor.plan(factory, session_id)
    started = threading.Event()
    release = threading.Event()

    class BlockingOrchestrator:
        def run_ready_tasks(self, factory, session_id):
            started.set()
            assert release.wait(5)
            return []

    monkeypatch.setattr(task_runner, "_semaphore", threading.BoundedSemaphore(1))
    monkeypatch.setattr(task_runner, "_build_orchestrator", lambda bus: BlockingOrchestrator())
    try:
        assert task_runner.kick_task(factory, bus, tasks[0])
        assert started.wait(5)
        assert not task_runner.kick_task(factory, bus, tasks[1])
        assert sum(t.is_alive() for t in task_runner._threads) == 1
    finally:
        release.set()
        task_runner.shutdown_runner(timeout=5)
