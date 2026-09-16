"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from agent_system.config import clear_settings_cache
from agent_system.infra.db import make_engine, make_session_factory
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Base


@pytest.fixture(scope="session", autouse=True)
def _migrated_database(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Migrate a disposable template, never the developer's database."""
    from alembic import command
    from alembic.config import Config

    backend_root = Path(__file__).resolve().parents[1]
    template = tmp_path_factory.mktemp("api-template") / "template.db"
    url = f"sqlite:///{template}"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DATABASE_URL", url)
        clear_settings_cache()
        config = Config(str(backend_root / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", url)
        command.upgrade(config, "head")
        yield template
    clear_settings_cache()


@pytest.fixture(autouse=True)
def _fresh_settings(
    _migrated_database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Drop the cached Settings around every test.

    ``get_settings()`` is ``lru_cache``d for the process lifetime, but many
    fixtures monkeypatch env vars (SKILLS_DIR, TELEGRAM_*, VAULT_PATH…) and
    then build the app. Without clearing the cache those overrides are
    silently ignored and the app under test reads whatever was cached by
    whichever test imported it first — the exact isolation bug that made the
    skills/telegram/cloud-drive contracts fail depending on test order.
    """
    import sqlite3

    target = tmp_path / "api.db"
    with sqlite3.connect(_migrated_database) as source, sqlite3.connect(target) as dest:
        source.backup(dest)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{target}")
    monkeypatch.setenv("TASK_RUNNER_RECOVERY_ENABLED", "false")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
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
