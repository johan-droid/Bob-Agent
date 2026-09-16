"""Integration tests — event bus persistence, ordering, replay (v3.1 §6, §17)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from agent_system.domain.events import Event
from agent_system.infra.db import make_engine, make_session_factory
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Base, EventRow


@pytest.fixture()
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'events.db'}"


@pytest.fixture()
def session(db_url: str) -> Iterator[Session]:
    engine = make_engine(db_url)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    session = factory()
    yield session
    session.close()
    engine.dispose()


def test_emit_assigns_monotonic_sequence(session: Session) -> None:
    bus = EventBus()
    e1 = bus.emit(Event(type="session.created", session_id="ses_1"), session)
    e2 = bus.emit(Event(type="task.created", session_id="ses_1"), session)
    e3 = bus.emit(Event(type="task.started", session_id="ses_1"), session)
    assert e1.sequence is not None and e2.sequence is not None and e3.sequence is not None
    assert e1.sequence < e2.sequence < e3.sequence


def test_events_persisted_and_replayable(session: Session) -> None:
    bus = EventBus()
    for i in range(5):
        bus.emit(Event(type="task.queued", payload={"n": i}), session)

    replayed = bus.replay_after(session, after_sequence=0)
    assert len(replayed) == 5
    assert [e.payload["n"] for e in replayed] == [0, 1, 2, 3, 4]
    assert [e.sequence for e in replayed] == sorted(e.sequence for e in replayed)  # type: ignore[list-item]


def test_resume_after_sequence(session: Session) -> None:
    bus = EventBus()
    emitted = [bus.emit(Event(type="task.queued", payload={"n": i}), session) for i in range(10)]
    mid = emitted[4].sequence
    missed = bus.replay_after(session, after_sequence=int(mid))
    assert len(missed) == 5
    assert missed[0].payload["n"] == 5


def test_replay_filters_by_type(session: Session) -> None:
    bus = EventBus()
    bus.emit(Event(type="task.queued"), session)
    bus.emit(Event(type="model.completed"), session)
    bus.emit(Event(type="task.started"), session)
    tasks = bus.replay_after(session, after_sequence=0, event_type="task.queued")
    assert len(tasks) == 1
    assert tasks[0].type == "task.queued"


def test_secrets_redacted_at_emit(session: Session) -> None:
    bus = EventBus()
    event = bus.emit(
        Event(type="tool.completed", payload={"output": "ok", "api_key": "sk-secret"}),
        session,
    )
    assert event.payload["api_key"] == "[REDACTED]"
    row = session.query(EventRow).filter_by(event_id=event.event_id).one()
    assert row.payload["api_key"] == "[REDACTED]"


def test_subscriber_receives_but_cannot_break_emit(session: Session) -> None:
    bus = EventBus()
    received: list[Event] = []

    def boom(_event: Event) -> None:
        raise RuntimeError("subscriber crashed")

    bus.subscribe_all(received.append)
    bus.subscribe_all(boom)
    event = bus.emit(Event(type="session.created"), session)
    assert len(received) == 1
    assert received[0].event_id == event.event_id


def test_sequence_survives_reconnect(session: Session) -> None:
    """A NEW EventBus instance (process restart) continues the stored sequence."""
    bus = EventBus()
    last = bus.emit(Event(type="session.created"), session)
    session.commit()

    bus2 = EventBus()
    nxt = bus2.emit(Event(type="session.completed"), session)
    assert nxt.sequence == int(last.sequence) + 1  # type: ignore[arg-type]
