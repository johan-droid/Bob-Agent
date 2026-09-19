"""Alembic environment — autogenerate against ORM metadata."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool

from agent_system.infra.db import _normalize_url, make_engine
from agent_system.infra.models import Base

try:
    config = context.config
    if config is not None and config.config_file_name is not None:
        fileConfig(config.config_file_name)
except (AttributeError, NameError):
    config = None

target_metadata = Base.metadata


def _url() -> str:
    import os

    raw = os.environ.get("DATABASE_URL")
    if not raw and config is not None:
        raw = config.get_main_option("sqlalchemy.url")
    return _normalize_url(raw or "")


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = make_engine(_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # required for SQLite ALTERs
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if getattr(context, "config", None) is not None:
    if context.is_offline_mode():
        run_migrations_offline()
    else:
        run_migrations_online()
