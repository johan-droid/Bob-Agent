"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from agent_system.config import clear_settings_cache, get_settings
from agent_system.infra.db import make_engine, make_session_factory
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Base


@pytest.fixture(scope="session", autouse=True)
def _migrated_database() -> None:
    """Bring the configured database up to the current schema, once per run.

    API contract tests exercise the real ``app`` wiring, which uses
    ``settings.database_url`` rather than a per-test engine. Schema drift there
    (a model column added without the local database being migrated) would
    otherwise surface as unrelated-looking failures. Running the real
    migrations keeps the fixture honest: it applies exactly the migrations a
    deployment would.
    """
    from alembic import command
    from alembic.config import Config

    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", get_settings().database_url)
    command.upgrade(config, "head")


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    """Drop the cached Settings around every test.

    ``get_settings()`` is ``lru_cache``d for the process lifetime, but many
    fixtures monkeypatch env vars (SKILLS_DIR, TELEGRAM_*, VAULT_PATH…) and
    then build the app. Without clearing the cache those overrides are
    silently ignored and the app under test reads whatever was cached by
    whichever test imported it first — the exact isolation bug that made the
    skills/telegram/cloud-drive contracts fail depending on test order.
    """
    clear_settings_cache()
    yield
    clear_settings_cache()


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
