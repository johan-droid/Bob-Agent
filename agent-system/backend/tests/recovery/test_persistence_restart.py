"""Recovery tests — persistence survives restarts (v3.1 §11, Phase 1 acceptance).

Simulates a process crash by closing the engine mid-stream and reopening the
database file with a fresh engine. WAL mode must keep the DB uncorrupted and
all committed data intact.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy.exc

from agent_system.domain import ids
from agent_system.domain.events import Event
from agent_system.domain.tasks import TaskState
from agent_system.infra.db import make_engine, make_session_factory, session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Base, Task
from agent_system.infra.models import Session as Session_row


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "durable.db"


def _open(db_path: Path) -> tuple[object, object]:
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    return engine, make_session_factory(engine)


def test_task_survives_restart(db_path: Path) -> None:
    task_id = ids.new_task_id()

    # "Process 1": create a task, transition to QUEUED, commit, then die.
    engine1, factory1 = _open(db_path)
    with session_scope(factory1) as session:  # type: ignore[arg-type]
        session.add(Session_row(id="ses_1", goal="test goal"))
        session.add(
            Task(
                id=task_id,
                session_id="ses_1",
                task_type="code",
                title="fix tests",
                state=TaskState.QUEUED.value,
            )
        )
    engine1.dispose()

    # "Process 2": reopen the same file — task must be there, still QUEUED.
    engine2, factory2 = _open(db_path)
    with session_scope(factory2) as session:  # type: ignore[arg-type]
        task = session.get(Task, task_id)
        assert task is not None
        assert task.state == TaskState.QUEUED.value
    engine2.dispose()


def test_events_survive_restart_and_sequence_continues(db_path: Path) -> None:
    engine1, factory1 = _open(db_path)
    bus1 = EventBus()
    with session_scope(factory1) as session:  # type: ignore[arg-type]
        bus1.emit(Event(type="session.created", session_id="ses_9"), session)
        bus1.emit(Event(type="task.created", session_id="ses_9"), session)
    engine1.dispose()

    engine2, factory2 = _open(db_path)
    bus2 = EventBus()
    with session_scope(factory2) as session:  # type: ignore[arg-type]
        replayed = bus2.replay_after(session, after_sequence=0)
        assert [e.type for e in replayed] == ["session.created", "task.created"]
        nxt = bus2.emit(Event(type="task.started", session_id="ses_9"), session)
        assert nxt.sequence == 3
    engine2.dispose()


def test_wal_uncorrupted_after_kill_mid_session(db_path: Path) -> None:
    """A crashed (uncommitted) session must not corrupt or partially write."""
    engine1, factory1 = _open(db_path)
    # Seed parent session in its own committed transaction (crash test targets
    # the task insert, not FK setup).
    with session_scope(factory1) as session:  # type: ignore[arg-type]
        session.add(Session_row(id="ses_1", goal="test goal"))
    with pytest.raises(RuntimeError):
        with session_scope(factory1) as session:  # type: ignore[arg-type]
            session.add(
                Task(
                    id=ids.new_task_id(),
                    session_id="ses_1",
                    task_type="code",
                    title="will crash",
                )
            )
            session.flush()
            raise RuntimeError("simulated crash mid-transaction")
    engine1.dispose()

    engine2, factory2 = _open(db_path)
    with session_scope(factory2) as session:  # type: ignore[arg-type]
        assert session.query(Task).count() == 0  # rolled back cleanly
        # DB still fully usable:
        session.add(
            Task(
                id=ids.new_task_id(),
                session_id="ses_1",
                task_type="code",
                title="after crash",
                state=TaskState.PENDING.value,
            )
        )
    engine2.dispose()


def test_idempotency_key_unique_constraint(db_path: Path) -> None:
    engine, factory = _open(db_path)
    key = "create-workspace:abc123"
    with session_scope(factory) as session:  # type: ignore[arg-type]
        session.add(Session_row(id="ses_1", goal="test goal"))
    with session_scope(factory) as session:  # type: ignore[arg-type]
        session.add(
            Task(
                id=ids.new_task_id(),
                session_id="ses_1",
                task_type="code",
                title="first",
                idempotency_key=key,
            )
        )
    with pytest.raises(sqlalchemy.exc.IntegrityError):  # duplicate idempotency key
        with session_scope(factory) as session:  # type: ignore[arg-type]
            session.add(
                Task(
                    id=ids.new_task_id(),
                    session_id="ses_1",
                    task_type="code",
                    title="duplicate",
                    idempotency_key=key,
                )
            )
    engine.dispose()
