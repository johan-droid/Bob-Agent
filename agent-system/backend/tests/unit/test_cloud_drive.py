"""Cloud driver — in-process session execution without Redis/RQ.

Acceptance: empty sessions get one llm task; drive runs it to SUCCEEDED
offline (echo provider, no network); retry moves FAILED -> QUEUED and
refuses non-retryable states; drive is idempotent on re-run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_system.domain.tasks import TaskState
from agent_system.infra.db import make_engine, make_session_factory, session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Base, Session, Task
from agent_system.services.cloud import (
    drive_session,
    ensure_session_tasks,
    retry_task_queued,
)
from agent_system.services.orchestrator import Supervisor


def _factory(tmp_path: Path) -> Any:
    engine = make_engine(f"sqlite:///{tmp_path / 'cloud.db'}")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _session(factory: Any, goal: str = "cloud test goal") -> str:
    return Supervisor(EventBus()).create_session(factory, goal)


class TestEnsureSessionTasks:
    def test_creates_one_llm_task(self, tmp_path: Path) -> None:
        factory = _factory(tmp_path)
        session_id = _session(factory)
        ids = ensure_session_tasks(factory, EventBus(), session_id)
        assert len(ids) == 1
        with session_scope(factory) as db:
            row = db.get(Task, ids[0])
            assert row is not None
            assert row.input_json == {"goal": "cloud test goal"}
            assert row.state == TaskState.QUEUED.value

    def test_idempotent_when_tasks_exist(self, tmp_path: Path) -> None:
        factory = _factory(tmp_path)
        session_id = _session(factory)
        first = ensure_session_tasks(factory, EventBus(), session_id)
        second = ensure_session_tasks(factory, EventBus(), session_id)
        assert first == second

    def test_unknown_session_raises(self, tmp_path: Path) -> None:
        factory = _factory(tmp_path)
        try:
            ensure_session_tasks(factory, EventBus(), "ses_missing00000000000001")
        except LookupError:
            return
        raise AssertionError("expected LookupError")


class TestDriveSession:
    def test_drives_to_succeeded_offline(self, tmp_path: Path, monkeypatch: Any) -> None:
        vault = tmp_path / "vault"
        monkeypatch.setenv("VAULT_PATH", str(vault))
        monkeypatch.setenv("DEFAULT_PROVIDER", "echo")
        # .env.local on dev machines may pin a real provider/model — clear
        # both so the drive runs the deterministic offline echo path.
        monkeypatch.setenv("DEFAULT_MODEL", "")
        monkeypatch.setenv("MEMORY_RECALL_TOP_K", "0")
        monkeypatch.setenv("MEMORY_AUTO_REMEMBER", "true")
        factory = _factory(tmp_path)
        session_id = _session(factory)
        summary = drive_session(factory, EventBus(), session_id)
        assert summary["session_id"] == session_id
        assert summary["total"] == 1
        assert summary["succeeded"] == 1
        assert summary["failed"] == 0
        # Outcome auto-persisted to the (redirected) vault.
        assert list(vault.rglob("*.md"))
        # Re-drive is quiescent (no duplicate tasks, no re-runs).
        again = drive_session(factory, EventBus(), session_id)
        assert again["total"] == 1
        assert again["succeeded"] == 1

    def test_drive_empty_goal_session(self, tmp_path: Path, monkeypatch: Any) -> None:
        monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
        monkeypatch.setenv("DEFAULT_PROVIDER", "echo")
        monkeypatch.setenv("DEFAULT_MODEL", "")
        monkeypatch.setenv("MEMORY_RECALL_TOP_K", "0")
        factory = _factory(tmp_path)
        bus = EventBus()
        with session_scope(factory) as db:
            db.add(Session(id="ses_empty0000000000000001", goal="", status="ACTIVE"))
        summary = drive_session(factory, bus, "ses_empty0000000000000001")
        assert summary["total"] == 1


class TestRetryTaskQueued:
    def test_failed_to_queued(self, tmp_path: Path) -> None:
        factory = _factory(tmp_path)
        session_id = _session(factory)
        (task_id,) = ensure_session_tasks(factory, EventBus(), session_id)
        with session_scope(factory) as db:
            row = db.get(Task, task_id)
            assert row is not None
            row.state = TaskState.FAILED.value
        out_session = retry_task_queued(factory, EventBus(), task_id)
        assert out_session == session_id
        with session_scope(factory) as db:
            row = db.get(Task, task_id)
            assert row is not None
            assert row.state == TaskState.QUEUED.value

    def test_succeeded_not_retryable(self, tmp_path: Path) -> None:
        factory = _factory(tmp_path)
        session_id = _session(factory)
        (task_id,) = ensure_session_tasks(factory, EventBus(), session_id)
        with session_scope(factory) as db:
            row = db.get(Task, task_id)
            assert row is not None
            row.state = TaskState.SUCCEEDED.value
        try:
            retry_task_queued(factory, EventBus(), task_id)
        except Exception:
            return
        raise AssertionError("expected retry of SUCCEEDED to be rejected")

    def test_unknown_task_raises(self, tmp_path: Path) -> None:
        factory = _factory(tmp_path)
        try:
            retry_task_queued(factory, EventBus(), "task_missing000000000001")
        except LookupError:
            return
        raise AssertionError("expected LookupError")
