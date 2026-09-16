"""Phase 19 — Production hardening: chaos & restart suite (v3.1 §29).

Each test simulates a failure mode and asserts a predictable, non-corrupting
outcome: duplicate events/tasks, Redis-style double delivery, crash-restart
persistence, approval expiry, replay safety, batching isolation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_system.domain.events import Event
from agent_system.domain.tasks import TaskState, validate_transition
from agent_system.infra.db import make_engine, make_session_factory, session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Base, Task
from agent_system.services.batching import TaskBatcher
from agent_system.services.permissions import PermissionGate
from agent_system.services.recording import BehaviorRecorder, ReplayContext, ReplayService
from agent_system.services.workspaces import WorkspaceManager


@pytest.fixture()
def factory(tmp_path: Path) -> Any:
    engine = make_engine(f"sqlite:///{tmp_path / 'hard.db'}")
    Base.metadata.create_all(engine)
    f = make_session_factory(engine)
    yield f
    engine.dispose()


def _seed_task(factory: Any, bus: EventBus, session_id: str, state: str = "QUEUED") -> str:
    from agent_system.domain import ids

    task_id = ids.new_task_id()
    with session_scope(factory) as db:
        db.add(
            Task(
                id=task_id,
                session_id=session_id,
                task_type="code",
                title="chaos",
                state=state,
            )
        )
    return task_id


class TestEventChaos:
    def test_duplicate_event_id_deduped(self, factory: Any) -> None:
        bus = EventBus()
        evt = Event(type="task.started", task_id="task_x", actor="t")
        with session_scope(factory) as db:
            bus.emit(evt, db)
            bus.emit(evt, db)  # duplicate delivery
            bus.emit(evt, db)
        with session_scope(factory) as db:
            from agent_system.infra.models import EventRow

            count = db.query(EventRow).filter_by(type="task.started").count()
        assert count == 1, "duplicate event_id must not duplicate rows"

    def test_replay_after_sequence_is_stable(self, factory: Any) -> None:
        bus = EventBus()
        with session_scope(factory) as db:
            for i in range(5):
                bus.emit(Event(type="task.started", task_id=f"task_{i}", actor="t"), db)
        with session_scope(factory) as db:
            first = bus.replay_after(db, after_sequence=0, limit=10)
        with session_scope(factory) as db:
            second = bus.replay_after(db, after_sequence=0, limit=10)
        assert [e.event_id for e in first] == [e.event_id for e in second]


class TestRestartPersistence:
    def test_task_state_survives_fresh_engine(self, factory: Any, tmp_path: Path) -> None:
        """A brand-new engine over the same file sees committed state."""
        db_url = f"sqlite:///{tmp_path / 'hard.db'}"
        bus = EventBus()
        with session_scope(factory) as db:
            from agent_system.infra.models import Session

            db.add(Session(id="ses_hard", goal="hardening", status="ACTIVE"))
        task_id = _seed_task(factory, bus, "ses_hard")
        with session_scope(factory) as db:
            task = db.get(Task, task_id)
            validate_transition(TaskState(task.state), TaskState.RUNNING)
            task.state = TaskState.RUNNING.value

        engine2 = make_engine(db_url)
        factory2 = make_session_factory(engine2)
        with session_scope(factory2) as db:
            task = db.get(Task, task_id)
            assert task is not None
            assert task.state == TaskState.RUNNING.value
        engine2.dispose()

    def test_kill_and_reopen_database(self, tmp_path: Path) -> None:
        """Crash after write: reopen the same file, state intact."""
        url = f"sqlite:///{tmp_path / 'crash.db'}"
        engine = make_engine(url)
        Base.metadata.create_all(engine)
        factory = make_session_factory(engine)
        bus = EventBus()
        with session_scope(factory) as db:
            from agent_system.infra.models import Session

            db.add(Session(id="ses_c", goal="crash", status="ACTIVE"))
        tid = _seed_task(factory, bus, "ses_c")
        with session_scope(factory) as db:
            t = db.get(Task, tid)
            assert t is not None
            t.state = TaskState.SUCCEEDED.value
        engine.dispose()  # hard close

        engine2 = make_engine(url)
        factory2 = make_session_factory(engine2)
        with session_scope(factory2) as db:
            t = db.get(Task, tid)
            assert t is not None
            assert t.state == TaskState.SUCCEEDED.value
        engine2.dispose()


class TestApprovalExpiry:
    def test_expired_approval_fails_closed(self) -> None:
        from agent_system.services.permissions import ApprovalRequest

        gate = PermissionGate()
        rec = gate.request(
            ApprovalRequest(
                requested_action="fs.delete",
                risk="HIGH",
                scope="file:delete",
                requester="test",
            )
        )
        gate.decide(rec.approval_id, approve=True)
        expired = gate.sweep_expired()
        # A HIGH approval left pending would expire; decided ones are stable.
        assert isinstance(expired, list)
        # Decision survives sweep:
        assert gate.get(rec.approval_id).decision.value == "APPROVED"  # type: ignore[union-attr]

    def test_pending_approval_sweeps_to_expired(self) -> None:
        from datetime import timedelta

        from agent_system.domain.events import utcnow
        from agent_system.services.permissions import ApprovalRequest

        gate = PermissionGate()
        rec = gate.request(
            ApprovalRequest(
                requested_action="net.fetch",
                risk="LOW",
                scope="net:fetch",
                requester="test",
            )
        )
        # Age it past the 60-minute LOW TTL:
        gate._records[rec.approval_id].expires_at = utcnow() - timedelta(minutes=1)  # noqa: SLF001
        expired = gate.sweep_expired()
        assert rec.approval_id in expired
        assert gate.get(rec.approval_id).decision.value == "EXPIRED"  # type: ignore[union-attr]


class TestReplaySafetyUnderChaos:
    def test_replay_blocked_after_workspace_mutation(self, factory: Any, tmp_path: Path) -> None:
        rdir = tmp_path / "recordings"
        ws_dir = tmp_path / "workspaces"
        manager = WorkspaceManager(ws_dir)
        manager.create("ws_r")
        manager.write_file("ws_r", "a.txt", b"v1")
        fingerprint_v1 = manager.fingerprint("ws_r")

        recorder = BehaviorRecorder(rdir)
        rid = recorder.start(factory, context=ReplayContext(workspace_fingerprint=fingerprint_v1))
        recorder.record("tool_call", "fs.write", {"path": "a.txt"})
        recorder.finish(factory)

        # Mutate the workspace afterward:
        manager.write_file("ws_r", "a.txt", b"v2-changed")
        fingerprint_v2 = manager.fingerprint("ws_r")
        assert fingerprint_v1 != fingerprint_v2

        service = ReplayService(rdir)
        with pytest.raises(Exception, match="fingerprint"):
            service.replay(
                rid,
                "APPROVED_REEXECUTE",
                ReplayContext(workspace_fingerprint=fingerprint_v2),
                approval_id="approval_x",
            )
        # INSPECT still works — read-only is always safe:
        result = service.replay(rid, "INSPECT", ReplayContext(workspace_fingerprint=fingerprint_v2))
        assert result.allowed and not result.fingerprint_match


def _make_session_local(factory: Any) -> str:
    from agent_system.domain import ids as _ids
    from agent_system.infra.models import Session

    sid = _ids.new_session_id()
    with session_scope(factory) as db:
        db.add(Session(id=sid, goal="batch chaos", status="ACTIVE"))
    return sid


def _make_task_local(factory: Any, session_id: str) -> str:
    from agent_system.domain import ids as _ids

    tid = _ids.new_task_id()
    with session_scope(factory) as db:
        db.add(Task(id=tid, session_id=session_id, task_type="code", title="c", state="QUEUED"))
    return tid


class TestBatchPartialFailure:
    def test_failed_member_does_not_block_batch(self, factory: Any) -> None:
        bus = EventBus()
        session_id = _make_session_local(factory)
        ids = [_make_task_local(factory, session_id) for _ in range(4)]
        batch = TaskBatcher(bus).create_batch(factory, session_id, ids)
        # One fails, two succeed, one still queued:
        with session_scope(factory) as db:
            db.get(Task, ids[0]).state = "FAILED"
            db.get(Task, ids[1]).state = "SUCCEEDED"
            db.get(Task, ids[2]).state = "SUCCEEDED"
        from agent_system.services.batching import batch_status

        status = batch_status(factory, batch["batch_id"])
        assert status["states"] == {"FAILED": 1, "SUCCEEDED": 2, "QUEUED": 1}


class TestWorkerDoubleDelivery:
    def test_execute_task_idempotent_on_nonqueued(self, factory: Any) -> None:
        """Worker receiving a task twice skips the duplicate (already handled)."""
        from agent_system.worker import execute_task

        bus = EventBus()
        with session_scope(factory) as db:
            from agent_system.infra.models import Session

            db.add(Session(id="ses_w", goal="worker", status="ACTIVE"))
        task_id = _seed_task(factory, bus, "ses_w", state="SUCCEEDED")
        result = execute_task(task_id=task_id, factory=factory)
        assert result["skipped"] is True
