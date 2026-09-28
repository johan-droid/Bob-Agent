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


#: Provider/Telegram credentials the suite must never inherit from
#: ``backend/.env.local``. Settings reads that file by default, so without this
#: every TestClient lifespan long-polls the developer's real bot and every
#: contract test can make a billable LLM call. Emptying them keeps the code
#: paths (key-missing branches) intact while guaranteeing no real call.
_REAL_CREDENTIAL_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "TELEGRAM_WEBHOOK_URL",
    "OPENROUTER_API_KEY",
    "NIM_API_KEY",
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "TOGETHER_API_KEY",
    "MISTRAL_API_KEY",
    "DEEPSEEK_API_KEY",
    "HUGGINGFACE_API_KEY",
    "TOKENROUTER_API_KEY",
    "OPENCODE_API_KEY",
    "OLLAMA_CLOUD_API_KEY",
    "OPENCONNECTOR_API_KEY",
)


@pytest.fixture(scope="session", autouse=True)
def _no_real_credentials() -> Iterator[None]:
    """Strip real credentials from the environment for the whole session.

    ``Settings`` loads ``.env``/``.env.local`` (config.py:24) and
    ``TestClient(app)`` runs the app lifespan, which starts the Telegram
    poller. That produced 288 real outbound connections per suite run —
    api.telegram.org, api.openrouter.ai, api.github.com — using the
    developer's live keys, racing a locally running bot (HTTP 409) and
    consuming its pending updates.
    """
    with pytest.MonkeyPatch.context() as patch:
        for name in _REAL_CREDENTIAL_VARS:
            patch.setenv(name, "")
        # Planner/verifier LLM paths would otherwise call a real model.
        patch.setenv("PLANNER_USE_LLM", "false")
        patch.setenv("VERIFIER_USE_LLM_JUDGE", "false")
        patch.setenv("DEFAULT_PROVIDER", "echo")
        yield
    clear_settings_cache()


@pytest.fixture(autouse=True)
def _reset_singletons() -> Iterator[None]:
    """Reset process-wide singletons around every test.

    ``_policy_engine``, ``INFERENCE_LOCKS``, ``INFERENCE_HEALTH`` and
    ``GLOBAL_HEALTH_TRACKER`` are module-level and outlive a test. A rate
    limit recorded by one test silently removed a provider from the candidate
    chain (``model_router._is_routable``) for every later test in the run.
    """
    from agent_system.services.inference_runtime import INFERENCE_HEALTH, INFERENCE_LOCKS
    from agent_system.services.policy import reset_policy_engine

    def _reset() -> None:
        INFERENCE_LOCKS.clear_all()
        INFERENCE_HEALTH.reset()
        reset_policy_engine()

    _reset()
    yield
    _reset()


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
