"""Deployment validation suite for Heroku cloud runtime."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_system.config import Settings, clear_settings_cache, get_settings
from agent_system.infra.db import _normalize_url


def test_clean_imports() -> None:
    """Verify primary application modules import cleanly without errors."""
    import agent_system.api.main as main_mod
    import agent_system.worker as worker_mod

    assert main_mod.app is not None
    assert callable(worker_mod.execute_task)
    assert Settings is not None


def test_port_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify Heroku $PORT environment variable overrides default api_port."""
    monkeypatch.setenv("PORT", "54321")
    clear_settings_cache()
    try:
        settings = get_settings()
        assert settings.effective_port == 54321
    finally:
        clear_settings_cache()


def test_postgres_url_normalization() -> None:
    """Verify Heroku postgres:// URLs are converted to postgresql:// for SQLAlchemy."""
    raw_heroku = "postgres://user:pass@ec2.compute.amazonaws.com:5432/dbname"
    normalized = _normalize_url(raw_heroku)
    assert normalized == "postgresql://user:pass@ec2.compute.amazonaws.com:5432/dbname"

    raw_psql = "postgresql://user:pass@localhost:5432/dbname"
    assert _normalize_url(raw_psql) == raw_psql


def test_alembic_url_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify Alembic env._url() normalizes postgres:// URLs."""

    monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@localhost:5432/testdb")
    # Import env module's _url helper
    import sys
    from pathlib import Path

    import agent_system.infra.db  # noqa: F401

    backend_dir = Path(__file__).resolve().parents[2]
    alembic_dir = backend_dir / "alembic"
    if str(alembic_dir) not in sys.path:
        sys.path.insert(0, str(alembic_dir))
    import env  # type: ignore

    assert env._url() == "postgresql://user:pass@localhost:5432/testdb"


def test_production_secrets_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify AGENT_ENV=production refuses to start with default secrets."""
    monkeypatch.setenv("AGENT_ENV", "production")
    monkeypatch.setenv("API_SESSION_SECRET", "dev-only-secret-change-me")
    clear_settings_cache()
    try:
        with pytest.raises(RuntimeError, match="Refusing to start with default dev secrets"):
            get_settings()
    finally:
        clear_settings_cache()


def test_production_secrets_pass_when_provided(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify AGENT_ENV=production succeeds when custom secrets are provided."""
    monkeypatch.setenv("AGENT_ENV", "production")
    monkeypatch.setenv("API_SESSION_SECRET", "prod-secret-1234567890-a1b2c3d4e5f6")
    monkeypatch.setenv("AGENT_BOOTSTRAP_SECRET", "prod-bootstrap-1234567890-a1b2c3d4e5f6")
    clear_settings_cache()
    try:
        s = get_settings()
        assert s.agent_env == "production"
    finally:
        clear_settings_cache()


def test_telegram_webhook_header_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify Telegram webhook authentication accepts both standard and legacy headers."""
    from agent_system.api.main import app

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u12345678")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "my-secret-webhook-token")
    clear_settings_cache()

    try:
        with TestClient(app) as client:
            # 1. Invalid secret -> 401
            resp = client.post(
                "/api/v1/telegram/webhook",
                json={"update_id": 100},
                headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
            )
            assert resp.status_code == 401

            # 2. Standard Telegram header X-Telegram-Bot-Api-Secret-Token -> accepted (pass auth)
            resp = client.post(
                "/api/v1/telegram/webhook",
                json={"update_id": 101},
                headers={"X-Telegram-Bot-Api-Secret-Token": "my-secret-webhook-token"},
            )
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok"}

            # 3. Legacy header X-Telegram-Webhook-Secret -> accepted
            resp = client.post(
                "/api/v1/telegram/webhook",
                json={"update_id": 102},
                headers={"X-Telegram-Webhook-Secret": "my-secret-webhook-token"},
            )
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok"}
    finally:
        clear_settings_cache()
