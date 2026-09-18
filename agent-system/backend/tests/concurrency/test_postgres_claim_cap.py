"""Concurrency — PostgreSQL integration for the claim cap (INV-010).

The SQLite proof (``test_live_execution_cap.py``) relies on SQLite's writer
serialization. This module re-proves the same invariant on a real PostgreSQL
server, where ``READ COMMITTED`` allows two claimants of DIFFERENT tasks to
interleave between reading the live-slot count and writing their claim — the
exact overshoot window the plain guarded UPDATE cannot close.

What is proven here:

- the claim seam takes ``pg_advisory_xact_lock`` (DB-level serialization), so
  concurrent claims of DIFFERENT tasks by independent DB sessions cannot
  overshoot ``MAX_CONCURRENT_TASKS``;
- the invariant holds with the *same* code path both drivers use
  (``claim_queued_task`` via ``Orchestrator._run_task`` and
  ``agent_system.worker.execute_task``);
- claims >> limit: 50 claim attempts against a cap of 2;
- observed simultaneous live executions (handlers BLOCKED mid-run, sampled
  from an independent connection) never exceed the limit.

Server selection: ``POSTGRES_TEST_URL`` if set, else a local server on
``localhost:55432`` (the CI/dev convention: ``docker run -p 55432:5432
postgres:16-alpine``). When no server answers, the module self-skips with the
reason printed — it never silently passes on SQLite.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import pytest
from sqlalchemy import text

from agent_system.agents import registry as agent_registry
from agent_system.config import clear_settings_cache
from agent_system.infra.db import make_engine, make_session_factory, session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Base, Session, Task
from agent_system.services.orchestrator import Orchestrator, Supervisor
from agent_system.worker import execute_task

LIVE_STATES = ("RUNNING", "REVIEW")
PG_LOCK_KEY = 721834917563402  # must match orchestrator._PG_CLAIM_LOCK_KEY

DEFAULT_PG_URL = "postgresql+psycopg2://pg:pg@localhost:55432/pgconc"


def _pg_url() -> str | None:
    url = os.environ.get("POSTGRES_TEST_URL", DEFAULT_PG_URL)
    try:
        import psycopg2  # noqa: F401

        probe = make_engine(url)
        try:
            with probe.connect() as conn:
                conn.execute(text("SELECT 1"))
        finally:
            probe.dispose()
    except Exception:
        return None
    return url


PG_URL = _pg_url()

pytestmark = pytest.mark.skipif(
    PG_URL is None,
    reason="no PostgreSQL server (set POSTGRES_TEST_URL or run: "
    "docker run -d --rm -p 55432:5432 -e POSTGRES_PASSWORD=pg "
    "-e POSTGRES_USER=pg -e POSTGRES_DB=pgconc postgres:16-alpine)",
)


@pytest.fixture()
def pg_factory() -> Any:
    assert PG_URL is not None
    engine = make_engine(PG_URL)

    # Independent-connection simulation: each sessionScope call gets its own
    # pooled connection, exactly like separate worker processes would.
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    fac = make_session_factory(engine)
    yield fac
    engine.dispose()


def _queued_tasks(factory: Any, session_id: str, n: int) -> list[str]:
    sup = Supervisor(EventBus())
    with session_scope(factory) as db:
        db.add(Session(id=session_id, goal="pg-conc", status="ACTIVE"))
    ids = [sup.add_task(factory, session_id, task_type="code", title=f"t{i}") for i in range(n)]
    with session_scope(factory) as db:
        for tid in ids:
            row = db.get(Task, tid)
            assert row is not None
            row.state = "QUEUED"
    return ids


def _live(factory: Any, session_id: str) -> int:
    with session_scope(factory) as db:
        return (
            db.query(Task)
            .filter(Task.session_id == session_id, Task.state.in_(LIVE_STATES))
            .count()
        )


def _wait_count(sem: threading.Semaphore, count: int, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    for _ in range(count):
        if not sem.acquire(timeout=max(0.05, deadline - time.monotonic())):
            return False
    return True


class TestPostgresClaimCap:
    def test_advisory_lock_serializes_independent_claim_sessions(
        self, pg_factory: Any, monkeypatch: Any
    ) -> None:
        """5 independent DB sessions claim 5 DIFFERENT tasks simultaneously
        against cap 2 — at most 2 may win (the overshoot window on PG)."""
        monkeypatch.setenv("MAX_CONCURRENT_TASKS", "2")
        clear_settings_cache()
        factory = pg_factory
        session_id = "ses_pgcap00000000000001"
        ids = _queued_tasks(factory, session_id, 5)

        # Advisory lock must be present at the seam (defense against drift).
        import inspect

        from agent_system.services import orchestrator as orch_mod

        assert "pg_advisory_xact_lock" in inspect.getsource(orch_mod.claim_queued_task)

        n = 5
        barrier = threading.Barrier(n)
        wins = 0
        lock = threading.Lock()

        def claim(task_id: str) -> None:
            nonlocal wins
            # Own session (own pooled connection) = independent process stand-in.
            barrier.wait(timeout=30)
            with session_scope(factory) as db:
                from agent_system.services.orchestrator import claim_queued_task

                ok = claim_queued_task(db, task_id)
            if ok:
                with lock:
                    wins += 1

        threads = [threading.Thread(target=claim, args=(t,)) for t in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert wins == 2, f"expected exactly 2 claims to win under cap, got {wins}"
        assert _live(factory, session_id) == 2
        clear_settings_cache()

    def test_50_claim_attempts_live_executions_never_exceed_cap(
        self, pg_factory: Any, monkeypatch: Any
    ) -> None:
        """50 claim attempts >> cap 2: observed simultaneous RUNNING never > 2.

        Every claim attempt runs through the exact production seam. Half the
        attempts enter via the orchestrator, half via the RQ worker body, so
        both drivers are proven against one live PostgreSQL server.
        """
        monkeypatch.setenv("MAX_CONCURRENT_TASKS", "2")
        clear_settings_cache()
        from agent_system.config import get_settings

        assert get_settings().max_concurrent_tasks == 2
        factory = pg_factory
        session_id = "ses_pgcap00000000000002"
        attempts = 50
        ids = _queued_tasks(factory, session_id, attempts)

        release = threading.Event()
        entered = threading.Semaphore(0)

        def blocked_handler(task_input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
            entered.release()
            assert release.wait(timeout=60), "test never released handlers"
            return {"ok": True}

        # Both drivers must resolve the SAME blocking handler: the worker
        # resolves via the global agent registry, the orchestrator via its
        # own instance handler map.
        agent_registry.register("code", blocked_handler)
        orch = Orchestrator(EventBus())
        orch.register_handler("code", blocked_handler)

        peak = 0
        lock = threading.Lock()
        stop_sampling = threading.Event()

        def sampler() -> None:
            nonlocal peak
            while not stop_sampling.is_set():
                live = _live(factory, session_id)
                with lock:
                    peak = max(peak, live)
                time.sleep(0.005)

        sampler_thread = threading.Thread(target=sampler, daemon=True)
        sampler_thread.start()

        started = threading.Barrier(attempts)

        def run_attempt(task_id: str, use_worker: bool) -> None:
            started.wait(timeout=60)
            if use_worker:
                try:
                    execute_task(task_id, factory=factory)
                except Exception:
                    pass  # refusal paths may surface as job errors — not cap breaches
            else:
                orch._run_task(factory, task_id)

        threads = [
            threading.Thread(target=run_attempt, args=(tid, i % 2 == 0))
            for i, tid in enumerate(ids)
        ]
        for t in threads:
            t.start()

        # While handlers block, exactly cap-many must be simultaneously live.
        assert _wait_count(entered, 2), "cap never admitted the limit on PostgreSQL"
        live_now = _live(factory, session_id)
        assert live_now == 2, f"expected exactly 2 simultaneous live executions, saw {live_now}"

        with lock:
            current_peak = peak
        assert current_peak <= 2, f"PG OVERSHOOT: peak live executions {current_peak} > 2"

        release.set()
        deadline = time.monotonic() + 60
        while _live(factory, session_id) and time.monotonic() < deadline:
            time.sleep(0.05)
        stop_sampling.set()
        sampler_thread.join(timeout=5)
        for t in threads:
            t.join(timeout=120)

        with lock:
            final_peak = peak
        assert final_peak <= 2, f"PG OVERSHOOT: peak live executions {final_peak} > 2"

        # Total successful executions == 2 slots x remaining attempts drained:
        # everything reaches a terminal state, nothing stranded.
        orch2 = Orchestrator(EventBus())
        orch2.register_handler("code", lambda i, c: {"ok": True})
        orch.register_handler("code", lambda i, c: {"ok": True})
        for _ in range(60):
            if not orch2.run_ready_tasks(factory, session_id):
                break
        with session_scope(factory) as db:
            states = [r.state for r in db.query(Task).filter_by(session_id=session_id).all()]
            running = (
                db.query(Task)
                .filter_by(session_id=session_id)
                .filter(Task.state.in_(LIVE_STATES))
                .count()
            )
        assert running == 0
        assert all(s in ("SUCCEEDED", "FAILED") for s in states), states
        clear_settings_cache()

    def test_pg_lock_key_matches_seam(self) -> None:
        """The advisory-lock key must be one fixed value shared by all claimants."""
        from agent_system.services.orchestrator import _PG_CLAIM_LOCK_KEY

        assert _PG_CLAIM_LOCK_KEY == PG_LOCK_KEY
