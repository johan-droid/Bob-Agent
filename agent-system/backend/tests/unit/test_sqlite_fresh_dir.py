"""Fresh-install regression — default SQLite URL must work on a clean clone.

The default ``DATABASE_URL`` is ``sqlite:///data/agent_system.db`` (relative to
the backend working directory) and ``data/`` is not tracked in git. A fresh
clone therefore has no ``data/`` directory, and every connection failed with
``sqlite3.OperationalError: unable to open database file`` — breaking Alembic
migrations, API startup, and the worker alike (DEF-002).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import text

from agent_system.infra.db import make_engine, make_session_factory, session_scope


def test_make_engine_creates_missing_parent_dir(tmp_path: Path, monkeypatch: Any) -> None:
    """A relative sqlite URL whose directory does not exist still connects."""
    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / "data").exists()
    engine = make_engine("sqlite:///data/agent_system.db")
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        db.execute(text("CREATE TABLE IF NOT EXISTS probe (x INTEGER)"))
        db.execute(text("INSERT INTO probe VALUES (1)"))
    assert (tmp_path / "data" / "agent_system.db").exists()
    engine.dispose()


def test_make_engine_creates_missing_parent_dir_absolute(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested" / "bob.db"
    assert not target.parent.exists()
    engine = make_engine(f"sqlite:///{target}")
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        db.execute(text("SELECT 1"))
    assert target.exists()
    engine.dispose()


def test_memory_and_uri_urls_do_not_create_dirs(tmp_path: Path) -> None:
    """``:memory:`` and URI-style URLs must not be treated as file paths."""
    engine = make_engine("sqlite:///:memory:")
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        db.execute(text("SELECT 1"))
    engine.dispose()
