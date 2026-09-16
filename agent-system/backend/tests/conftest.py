"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from agent_system.infra.db import make_engine, make_session_factory
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Base


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[Session]:
    """A real SQLite file DB (WAL) per test — not in-memory, so restart
    behavior is actually exercised."""
    url = f"sqlite:///{tmp_path / 'test.db'}"
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    session = factory()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def event_bus() -> EventBus:
    return EventBus()
