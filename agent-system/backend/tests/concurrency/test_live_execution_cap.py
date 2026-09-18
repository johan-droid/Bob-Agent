"""Concurrency — live-execution cap proof on SQLite (INV-010).

The older adversarial proof (``tests/adversarial/test_audit_p0_completion.py``)
asserts on the *post-hoc* count: with handlers that finish instantly, two
threads can run sequentially through the cap and the RUNNING count observed
AFTER the burst never exceeds it. That proves the claim refuses at the cap but
NOT that simultaneous executions stay within it.

This suite replaces that with a real concurrent execution proof:

- the handler BLOCKS on an event (a barrier of live handlers);
- N claim attempts >> limit fire simultaneously;
- the test counts tasks in live states (RUNNING/REVIEW) *while handlers are
  still blocked*, i.e. actual simultaneous live executions;
- the cap is exact: observed live executions NEVER exceed the limit.

Both execution drivers are proven to enforce the same invariant because both
claim through ``claim_queued_task``:

- the in-process Orchestrator (``Orchestrator._run_task``), and
- the RQ worker job body (``agent_system.worker.execute_task``).

Finally, a combined scenario mixes crash + lease expiry + recovery + retry +
concurrency in one run: a crashed (expired-lease) task is recovered and
re-executed while fresh claims race for the remaining capacity, and the live
count never exceeds the cap at any sampled instant.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event as sa_event

from agent_system.agents import registry as agent_registry
from agent_system.config import clear_settings_cache
from agent_system.domain.events import utcnow
from agent_system.domain.lifecycles import AgentState
from agent_system.infra.db import make_engine, make_session_factory, session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import AgentLease, AgentRun, Base, Session, Task
from agent_system.services.orchestrator import Orchestrator, Supervisor
from agent_system.worker import execute_task

LIVE_STATES = ("RUNNING", "REVIEW")  # REVIEW still holds a live slot (INV-010)


@pytest.fixture()
def cap2(monkeypatch: Any) -> int:
    monkeypatch.setenv("MAX_CONCURRENT_TASKS", "2")
    clear_settings_cache()
    yield 2
    clear_settings_cache()


@pytest.fixture()
def factory(tmp_path: Path) -> Any:
    engine = make_engine(f"sqlite:///{tmp_path / 'conc.db'}")

    # Thundering-herd headroom: 8 racing claimants each wait on the single
    # SQLite write lock; the 5s default busy timeout can be exhausted by the
    # queue. Production claims are sub-ms — this only pads the test burst.
    @sa_event.listens_for(engine, "connect")
    def _raise_busy_timeout(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

    Base.metadata.create_all(engine)
    fac = make_session_factory(engine)
    yield fac
    engine.dispose()


@pytest.fixture()
def clean_registry() -> Any:
    saved = agent_registry.snapshot()
    yield
    agent_registry.restore(saved)


def _queued_session(factory: Any, session_id: str, n: int) -> list[str]:
    sup = Supervisor(EventBus())
    with session_scope(factory) as db:
        db.add(Session(id=session_id, goal="conc", status="ACTIVE"))
    ids = [sup.add_task(factory, session_id, task_type="code", title=f"t{i}") for i in range(n)]
    with session_scope(factory) as db:
        for tid in ids:
            row = db.get(Task, tid)
            assert row is not None
            row.state = "QUEUED"
    return ids


def _live(factory: Any, session_id: str) -> int:
    """Count live executions RIGHT NOW (committed RUNNING/REVIEW rows)."""
    with session_scope(factory) as db:
        return (
            db.query(Task)
            .filter(Task.session_id == session_id, Task.state.in_(LIVE_STATES))
            .count()
        )


class _BlockedHandlers:
    """Handlers that block until released — proving they are SIMULTANEOUS.

    ``barrier`` releases up to ``n`` concurrent entrants; each entrant signals
    ``entered`` and then records the live DB count at its own peak moment.
    """

    def __init__(self, factory: Any, session_id: str, n: int) -> None:
        self.factory = factory
        self.session_id = session_id
        self.barrier = threading.Barrier(n)
        self.entered = threading.Semaphore(0)
        self.release = threading.Event()
        self.peak_seen = 0
        self._lock = threading.Lock()

    def __call__(self, task_input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.peak_seen = max(self.peak_seen, _live(self.factory, self.session_id))
        self.entered.release()
        # One slot of the barrier per *entrant*; the barrier size equals the
        # number of expected simultaneous entrants, so if MORE than the cap
        # ever entered, barrier.wait would deadlock the extras — instead we
        # use a timed wait so overshoot is observable, not fatal.
        try:
            self.barrier.wait(timeout=1.0)
        except threading.BrokenBarrierError:
            pass  # more entrants than barrier slots: overshoot evidence below
        assert self.release.wait(timeout=20), "test never released handlers"
        return {"ok": True}


def _wait_entered(entered: threading.Semaphore, count: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    for _ in range(count):
        if not entered.acquire(timeout=max(0.05, deadline - time.monotonic())):
            return False
    return True


class TestOrchestratorLiveCap:
    """Orchestrator path: 8 claimants >> cap 2, handlers block — peak live <= 2."""

    def test_simultaneous_live_executions_never_exceed_cap(
        self, factory: Any, cap2: int, clean_registry: Any
    ) -> None:
        session_id = "ses_orchlive0000000001"
        n = 8
        ids = _queued_session(factory, session_id, n)
        orch = Orchestrator(EventBus())
        blocked = _BlockedHandlers(factory, session_id, cap2)
        orch.register_handler("code", blocked)

        started = threading.Barrier(n)  # maximize claim contention
        results: dict[str, bool] = {}
        lock = threading.Lock()

        def run(task_id: str) -> None:
            started.wait(timeout=15)
            ok = orch._run_task(factory, task_id)
            with lock:
                results[task_id] = ok

        threads = [threading.Thread(target=run, args=(t,)) for t in ids]
        for t in threads:
            t.start()
        # Exactly `cap2` handlers must become simultaneously live.
        assert _wait_entered(blocked.entered, cap2), "cap did not admit the limit while blocked"
        peak = _live(factory, session_id)
        assert peak == cap2, f"expected exactly {cap2} simultaneous live executions, saw {peak}"
        assert blocked.peak_seen <= cap2, "an individual handler observed overshoot"
        blocked.release.set()  # let the two finish; losers already returned False
        for t in threads:
            t.join(timeout=30)

        claimed = [tid for tid, ok in results.items() if ok]
        # Invariant is SIMULTANEITY, not claims-per-burst: once the first two
        # handlers were released, waiting claimants legitimately won the freed
        # slots. What must hold: >= cap2 won while blocked, peak live == cap2
        # (asserted above), and every task drains to a terminal state.
        assert len(claimed) >= cap2, f"only {len(claimed)} claims ever won"
        for _ in range(25):
            if not orch.run_ready_tasks(factory, session_id):
                break
        with session_scope(factory) as db:
            states = [r.state for r in db.query(Task).filter_by(session_id=session_id).all()]
        assert all(s in ("SUCCEEDED", "FAILED") for s in states), states
        assert states.count("SUCCEEDED") == n  # losers were re-driven and finished


class TestWorkerLiveCap:
    """RQ worker path (execute_task): the same invariant from the same seam."""

    def test_worker_simultaneous_executions_never_exceed_cap(
        self, factory: Any, cap2: int, clean_registry: Any
    ) -> None:
        session_id = "ses_wrklive00000000001"
        n = 6
        ids = _queued_session(factory, session_id, n)
        blocked = _BlockedHandlers(factory, session_id, cap2)
        agent_registry.register("code", blocked)

        started = threading.Barrier(n)
        outcomes: list[str] = []
        lock = threading.Lock()

        def run(task_id: str) -> None:
            started.wait(timeout=15)
            try:
                result = execute_task(task_id, factory=factory)
                with lock:
                    outcomes.append("skipped" if result.get("skipped") else str(result["state"]))
            except BaseException as exc:  # noqa: BLE001 — any failure is a bug
                with lock:
                    outcomes.append(f"error:{type(exc).__name__}")

        threads = [threading.Thread(target=run, args=(t,)) for t in ids]
        for t in threads:
            t.start()
        assert _wait_entered(blocked.entered, cap2), "worker cap did not admit the limit"
        peak = _live(factory, session_id)
        assert peak == cap2, f"worker: expected exactly {cap2} live executions, saw {peak}"
        blocked.release.set()
        for t in threads:
            t.join(timeout=30)

        # Post-release claims may also win freed slots; simultaneity (peak
        # == cap2, above) is the invariant, not the per-burst win count.
        assert outcomes.count("SUCCEEDED") >= cap2, outcomes
        assert outcomes.count("skipped") + outcomes.count("SUCCEEDED") == n, outcomes


class TestWorkerVsOrchestratorSharedSeam:
    """Mixed drivers: orchestrator + worker claims contend on the same cap."""

    def test_mixed_drivers_never_exceed_cap(
        self, factory: Any, cap2: int, clean_registry: Any
    ) -> None:
        session_id = "ses_mixlive0000000001"
        n = 8
        ids = _queued_session(factory, session_id, n)
        blocked = _BlockedHandlers(factory, session_id, cap2)
        agent_registry.register("code", blocked)
        orch = Orchestrator(EventBus())

        started = threading.Barrier(n)
        entered_other = threading.Semaphore(0)

        def orch_run(task_id: str) -> None:
            started.wait(timeout=15)
            entered_other.release()
            orch._run_task(factory, task_id)

        def wrk_run(task_id: str) -> None:
            started.wait(timeout=15)
            entered_other.release()
            try:
                execute_task(task_id, factory=factory)
            except BaseException:
                pass  # a losing worker claim can raise via job semantics

        # Half enter through the orchestrator, half through the worker.
        jobs = [(tid, orch_run if i % 2 == 0 else wrk_run) for i, tid in enumerate(ids)]
        for tid, fn in jobs:
            threading.Thread(target=fn, args=(tid,), daemon=True).start()
        # Any `cap2` of the mixed entrants may hold the slots — the point is
        # that no MORE than cap2 ever do, whichever driver they came from.
        assert _wait_entered(entered_other, n, timeout=15)
        # Give claims a moment to settle, then sample repeatedly while the
        # first holders are still blocked inside their handlers.
        peaks = [_live(factory, session_id) for _ in range(20)]
        time.sleep(0.2)
        peaks.append(_live(factory, session_id))
        assert max(peaks) <= cap2, f"mixed-driver overshoot: peak live {max(peaks)}"
        blocked.release.set()
        # Wait for handlers to finish so the drain below owns all remaining work.
        deadline = time.monotonic() + 30
        while _live(factory, session_id) and time.monotonic() < deadline:
            time.sleep(0.05)
        orch2 = Orchestrator(EventBus())
        orch2.register_handler("code", lambda i, c: {"ok": True})
        for _ in range(25):
            if not orch2.run_ready_tasks(factory, session_id):
                break
        with session_scope(factory) as db:
            states = [r.state for r in db.query(Task).filter_by(session_id=session_id).all()]
        assert all(s in ("SUCCEEDED", "FAILED") for s in states), states


class TestCrashRecoveryRetryConcurrency:
    """Combined: crash + lease expiry + recovery + retry + concurrency."""

    def test_all_faults_mixed_live_cap_holds(
        self, factory: Any, cap2: int, clean_registry: Any
    ) -> None:
        session_id = "ses_mixed0000000000001"
        ids = _queued_session(factory, session_id, 5)

        # -- crash: task 0 is RUNNING with an EXPIRED lease (process died) ----
        with session_scope(factory) as db:
            db.get(Task, ids[0]).state = "RUNNING"  # type: ignore[union-attr]
            db.add(
                AgentRun(
                    id="run_crashlive0000001",
                    task_id=ids[0],
                    agent_type="code",
                    state=AgentState.RUNNING.value,
                    worker_id="dead-worker",
                )
            )
            db.add(
                AgentLease(
                    agent_run_id="run_crashlive0000001",
                    worker_id="dead-worker",
                    state=AgentState.RUNNING.value,
                    heartbeat_at=utcnow() - timedelta(minutes=10),
                    lease_expires_at=utcnow() - timedelta(minutes=5),
                )
            )

        orch = Orchestrator(EventBus(), heartbeat_interval=0.05)
        # Attempt 0 was already consumed by the crashed run; next retry is #2.
        with session_scope(factory) as db:
            db.get(Task, ids[0]).attempt = 1  # type: ignore[union-attr]

        # -- recovery + retry: the reaper requeues the orphan (attempt 1 < 3) --
        recovered = orch.recover_orphans(factory)
        assert recovered == [ids[0]]
        with session_scope(factory) as db:
            assert db.get(Task, ids[0]).state == "QUEUED"  # type: ignore[union-attr]

        # -- concurrency: remaining 4 QUEUED tasks race while the recovered ---
        # -- one also re-claims; 2 slots only, handlers block --------------
        blocked = _BlockedHandlers(factory, session_id, cap2)
        orch.register_handler("code", blocked)

        started = threading.Barrier(5)
        for tid in ids[1:]:
            threading.Thread(
                target=lambda t=tid: (started.wait(timeout=15), orch._run_task(factory, t)),
                daemon=True,
            ).start()
        threading.Thread(
            target=lambda: (started.wait(timeout=15), orch._run_task(factory, ids[0])),
            daemon=True,
        ).start()

        assert _wait_entered(blocked.entered, cap2), "cap did not admit the limit"
        peak = _live(factory, session_id)
        assert peak == cap2, f"expected exactly {cap2} live post-recovery executions, saw {peak}"
        blocked.release.set()
        deadline = time.monotonic() + 30
        while _live(factory, session_id) and time.monotonic() < deadline:
            time.sleep(0.05)

        # -- drive everything (retry winners + refused claims) to quiescence --
        orch.register_handler("code", lambda i, c: {"ok": True})
        for _ in range(25):
            if not orch.run_ready_tasks(factory, session_id):
                break
        with session_scope(factory) as db:
            rows = db.query(Task).filter_by(session_id=session_id).all()
            states = {r.id: r.state for r in rows}
            crashed = db.get(Task, ids[0])
            assert crashed is not None
            assert crashed.attempt == 2  # crashed run + the one successful retry
        assert all(s in ("SUCCEEDED", "FAILED", "REVIEW") for s in states.values()), states
        assert states[ids[0]] == "SUCCEEDED"  # recovered + retried to success
