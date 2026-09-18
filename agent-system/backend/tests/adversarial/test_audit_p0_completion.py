"""Adversarial — concurrency enforcement, cloud restart recovery, safe fallback.

Covers the three remaining audit P0s:

- P0#4 (INV-010): ``MAX_CONCURRENT_TASKS`` is *enforced*, not merely
  calculated. Racing drivers (cloud drive thread, task runner, RQ worker)
  claim through one atomic guarded UPDATE — actual live work never exceeds
  the configured limit.
- P0#5 (INV-011): a cloud dyno crash between ingest and completion cannot
  strand accepted work. A RUNNING task with an expired lease is reclaimed
  (RECOVERING -> QUEUED) and re-driven to a terminal state.
- P0#6 (INV-014 + honest bounds): the unplannable-goal fallback preserves
  the inferred risk instead of blindly reporting LOW, and ``drive_session``
  reports ``limit_reached`` instead of letting the safety bound masquerade
  as ordinary completion.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

from agent_system.config import clear_settings_cache
from agent_system.domain.events import utcnow
from agent_system.domain.tasks import TaskState
from agent_system.infra.db import make_engine, make_session_factory, session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import AgentLease, AgentRun, Base, Session, Task
from agent_system.services.cloud import (
    MAX_DRIVE_ROUNDS,
    drive_session,
    fallback_plan,
)
from agent_system.services.orchestrator import Orchestrator, Supervisor, claim_queued_task


def _factory(tmp_path: Path) -> Any:
    engine = make_engine(f"sqlite:///{tmp_path / 'p0.db'}")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _mk_tasks(factory: Any, session_id: str, n: int) -> list[str]:
    """Create n QUEUED tasks in one session (no dependencies)."""
    sup = Supervisor(EventBus())
    with session_scope(factory) as db:
        db.add(Session(id=session_id, goal="p0", status="ACTIVE"))
    ids: list[str] = []
    for i in range(n):
        ids.append(sup.add_task(factory, session_id, task_type="llm", title=f"t{i}"))
    with session_scope(factory) as db:
        for tid in ids:
            row = db.get(Task, tid)
            assert row is not None
            row.state = TaskState.QUEUED.value
    return ids


def _states(factory: Any, session_id: str) -> list[str]:
    with session_scope(factory) as db:
        return [r.state for r in db.query(Task).filter_by(session_id=session_id).all()]


class TestConcurrencyEnforcement:
    """P0#4: the cap is enforced atomically at the single claim seam."""

    def test_claim_refuses_when_at_cap(self, tmp_path: Path, monkeypatch: Any) -> None:
        monkeypatch.setenv("MAX_CONCURRENT_TASKS", "2")
        clear_settings_cache()
        factory = _factory(tmp_path)
        ids = _mk_tasks(factory, "ses_cap00000000000000001", 3)

        # Two tasks enter live slots out-of-band (lease-backed RUNNING work).
        with session_scope(factory) as db:
            for tid in ids[:2]:
                db.get(Task, tid).state = TaskState.RUNNING.value  # type: ignore[union-attr]
        # Third claim must be refused while the two live slots are held.
        # (The claim must run inside a session_scope: it is a transactional
        # UPDATE — exactly how the orchestrator and worker call it.)
        with session_scope(factory) as db:
            assert claim_queued_task(db, ids[2]) is False
        with session_scope(factory) as db:
            assert db.get(Task, ids[2]).state == TaskState.QUEUED.value  # type: ignore[union-attr]
        # A REVIEW slot counts as live too (result + heartbeat still held).
        with session_scope(factory) as db:
            db.get(Task, ids[0]).state = TaskState.REVIEW.value  # type: ignore[union-attr]
            db.get(Task, ids[1]).state = TaskState.REVIEW.value  # type: ignore[union-attr]
        with session_scope(factory) as db:
            assert claim_queued_task(db, ids[2]) is False

    def test_claim_succeeds_when_under_cap(self, tmp_path: Path, monkeypatch: Any) -> None:
        monkeypatch.setenv("MAX_CONCURRENT_TASKS", "2")
        clear_settings_cache()
        factory = _factory(tmp_path)
        ids = _mk_tasks(factory, "ses_cap00000000000000002", 1)
        with session_scope(factory) as db:
            assert claim_queued_task(db, ids[0]) is True
            assert db.get(Task, ids[0]).state == TaskState.RUNNING.value  # type: ignore[union-attr]

    def test_orchestrator_round_never_exceeds_cap(self, tmp_path: Path, monkeypatch: Any) -> None:
        """N > limit concurrent claimants: observed RUNNING never exceeds limit."""
        monkeypatch.setenv("MAX_CONCURRENT_TASKS", "2")
        clear_settings_cache()
        factory = _factory(tmp_path)
        n = 8
        ids = _mk_tasks(factory, "ses_cap00000000000000003", n)
        orch = Orchestrator(EventBus())
        peak = 0
        gate = threading.Barrier(n)

        def worker(task_id: str) -> None:
            nonlocal peak
            gate.wait()  # maximize contention
            if orch._run_task(factory, task_id):
                with session_scope(factory) as db:
                    live = (
                        db.query(Task)
                        .filter(
                            Task.session_id == "ses_cap00000000000000003",
                            Task.state.in_([TaskState.RUNNING.value, TaskState.REVIEW.value]),
                        )
                        .count()
                    )
                peak = max(peak, live)

        threads = [threading.Thread(target=worker, args=(t,)) for t in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert peak <= 2, f"concurrency cap breached: {peak} live slots"
        # Drain: claims refused at the burst's cap stayed QUEUED (correct —
        # a later round/sweep owns them). Drive to quiescence, then everything
        # must be terminal; nothing is left stranded mid-flight.
        for _ in range(MAX_DRIVE_ROUNDS):
            if not orch.run_ready_tasks(factory, "ses_cap00000000000000003"):
                break
        assert all(
            s in ("SUCCEEDED", "FAILED") for s in _states(factory, "ses_cap00000000000000003")
        )


class TestCloudRestartRecovery:
    """P0#5: accepted work survives a dyno crash (stale lease -> retry)."""

    def _crashed_task(self, factory: Any, session_id: str) -> str:
        (task_id,) = _mk_tasks(factory, session_id, 1)
        with session_scope(factory) as db:
            db.get(Task, task_id).state = TaskState.RUNNING.value  # type: ignore[union-attr]
            db.add(
                AgentRun(
                    id="run_crashed0000000001",
                    task_id=task_id,
                    agent_type="llm",
                    state="RUNNING",
                    worker_id="inproc",
                )
            )
            db.add(
                AgentLease(
                    agent_run_id="run_crashed0000000001",
                    worker_id="inproc",
                    state="RUNNING",
                    heartbeat_at=utcnow() - timedelta(minutes=10),
                    lease_expires_at=utcnow() - timedelta(minutes=5),  # expired: dyno died
                )
            )
        return task_id

    def test_drive_session_recovers_orphaned_task(self, tmp_path: Path, monkeypatch: Any) -> None:
        monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
        monkeypatch.setenv("DEFAULT_PROVIDER", "echo")
        monkeypatch.setenv("DEFAULT_MODEL", "")
        monkeypatch.setenv("MEMORY_RECALL_TOP_K", "0")
        factory = _factory(tmp_path)
        session_id = "ses_rec00000000000000001"
        task_id = self._crashed_task(factory, session_id)
        summary = drive_session(factory, EventBus(), session_id)
        # The crashed task was reclaimed and driven to a terminal state —
        # not stranded RUNNING forever.
        assert summary["total"] == 1
        assert not any(s == TaskState.RUNNING.value for s in _states(factory, session_id))
        assert task_id in summary["failed_task_ids"] or summary["succeeded"] == 1

    def test_recover_orphans_respects_retries_exhausted(self, tmp_path: Path) -> None:
        factory = _factory(tmp_path)
        session_id = "ses_rec00000000000000002"
        (task_id,) = _mk_tasks(factory, session_id, 1)
        with session_scope(factory) as db:
            task = db.get(Task, task_id)
            assert task is not None
            task.state = TaskState.RUNNING.value
            task.attempt = 3  # retries exhausted
            db.add(
                AgentRun(
                    id="run_crashed0000000002",
                    task_id=task_id,
                    agent_type="llm",
                    state="RUNNING",
                    worker_id="inproc",
                )
            )
            db.add(
                AgentLease(
                    agent_run_id="run_crashed0000000002",
                    worker_id="inproc",
                    state="RUNNING",
                    heartbeat_at=utcnow() - timedelta(minutes=10),
                    lease_expires_at=utcnow() - timedelta(minutes=5),
                )
            )
        recovered = Orchestrator(EventBus()).recover_orphans(factory)
        assert recovered == [task_id]
        with session_scope(factory) as db:
            assert db.get(Task, task_id).state == TaskState.FAILED.value  # type: ignore[union-attr]


class TestSafeFallback:
    """P0#6: fallback preserves risk; the drive bound is reported honestly."""

    def test_fallback_keeps_high_risk_for_sensitive_goal(self) -> None:
        plan = fallback_plan("Rotate production credentials and deploy them")
        assert plan.risk == "HIGH"
        assert plan.tasks[0].risk == "HIGH"

    def test_fallback_low_for_benign_goal(self) -> None:
        plan = fallback_plan("Write a haiku about databases")
        assert plan.risk == "LOW"

    def test_drive_session_reports_limit_reached(self, tmp_path: Path, monkeypatch: Any) -> None:
        """A session with permanently-unfinished work must not look complete."""
        monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
        factory = _factory(tmp_path)
        session_id = "ses_lim00000000000000001"
        (task_id,) = _mk_tasks(factory, session_id, 1)

        # Stuck RUNNING under a LIVE lease: not recoverable, not runnable —
        # the session can never go quiescent, so the round bound must stop it.
        with session_scope(factory) as db:
            db.get(Task, task_id).state = TaskState.RUNNING.value  # type: ignore[union-attr]
            db.add(
                AgentRun(
                    id="run_stuck00000000001",
                    task_id=task_id,
                    agent_type="llm",
                    state="RUNNING",
                    worker_id="inproc",
                )
            )
            db.add(
                AgentLease(
                    agent_run_id="run_stuck00000000001",
                    worker_id="inproc",
                    state="RUNNING",
                    heartbeat_at=utcnow(),
                    lease_expires_at=utcnow() + timedelta(hours=1),  # live lease
                )
            )
        summary = drive_session(factory, EventBus(), session_id, max_rounds=MAX_DRIVE_ROUNDS)
        # The wedged task must surface as unfinished — never as "all done".
        assert summary["unfinished"] == 1
        assert summary["succeeded"] == 0
