"""Database engine and session management.

SQLite is the authoritative durable store (v3.1 §15): WAL enabled, foreign
keys ON, busy timeout set, all writes transactional.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

_SQLITE_PRAGMAS = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA foreign_keys=ON;",
    "PRAGMA busy_timeout=5000;",
    "PRAGMA synchronous=NORMAL;",
)


def _normalize_url(database_url: str) -> str:
    """Normalize well-known URL variants to SQLAlchemy form.

    Heroku Postgres (and several operators) issue ``postgres://`` URLs;
    SQLAlchemy 2 only accepts ``postgresql://``. Normalize here — the
    single choke point used by the API, worker, Alembic env, and CLI.
    """
    if database_url.startswith("postgres://"):
        return "postgresql://" + database_url[len("postgres://") :]
    return database_url


def _ensure_sqlite_parent_dir(database_url: str) -> None:
    """Create the parent directory of a file-based SQLite database.

    Fresh installations start without ``data/`` (it is not tracked in git),
    and a relative ``sqlite:///data/agent_system.db`` URL then fails every
    connection with "unable to open database file" — breaking migrations and
    API startup alike. This runs in the single engine choke point used by the
    API, worker, Alembic, and CLI so the documented install just works.
    Absolute paths whose parent already exists are untouched; ``:memory:`` and
    URI-style URLs are ignored.
    """
    if database_url.startswith("sqlite") and not database_url.startswith(
        ("sqlite:///:memory:", "sqlite://?", "file:")
    ):
        # sqlite:///relative/path.db  |  sqlite:////absolute/path.db
        _, _, rest = database_url.partition("sqlite:///")
        if rest and not rest.startswith(":memory:"):
            db_path = Path(rest if database_url.startswith("sqlite:////") else Path.cwd() / rest)
            parent = db_path.parent
            if str(parent) not in ("", "."):
                os.makedirs(parent, exist_ok=True)


def make_engine(database_url: str) -> Engine:
    """Create an engine; apply required pragmas to every SQLite connection."""
    database_url = _normalize_url(database_url)
    is_sqlite = database_url.startswith("sqlite")
    if is_sqlite:
        _ensure_sqlite_parent_dir(database_url)
        engine = create_engine(
            database_url,
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
        )

        @event.listens_for(engine, "connect")
        def _set_pragmas(dbapi_connection, _record):  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            for pragma in _SQLITE_PRAGMAS:
                cursor.execute(pragma)
            cursor.close()

        return engine
    return create_engine(database_url, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Transactional scope: commit on success, rollback and re-raise on error."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
