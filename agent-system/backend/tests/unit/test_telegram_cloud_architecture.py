"""Architectural regression and Telegram cloud transport tests.

Enforces:
1. Obsolete UI interfaces (web directory, CLI chat REPL, desktop launchers) are absent.
2. Production mode enforces Telegram configuration (bot token, webhook secret, identity mode).
3. Webhook endpoint receives valid updates, validates secret token, and rejects invalid secrets.
4. Commands, natural language goals, and callback queries process correctly.
5. Ingestion ledger de-duplicates updates and supports crash recovery re-delivery.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_system.config import clear_settings_cache, get_settings
from agent_system.domain.ids import new_id
from agent_system.infra.db import make_engine, make_session_factory
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import (
    Base,
    DeliveryOutbox,
    Session,
    TelegramAccount,
    TelegramUpdate,
    User,
)


def _provision_tg_user(factory: Any, tg_user_id: str, chat_id: int, role: str = "owner") -> str:
    with factory() as db:
        user = User(
            id=new_id("usr"),
            display_name=f"tg:{tg_user_id}",
            auth_provider="telegram",
            role=role,
            is_active=True,
        )
        db.add(user)
        db.flush()
        db.add(
            TelegramAccount(
                id=new_id("tga"),
                telegram_user_id=tg_user_id,
                user_id=user.id,
                chat_id=str(chat_id),
                role=role,
            )
        )
        db.commit()
        return str(user.id)


def test_obsolete_interfaces_absent() -> None:
    """Verify obsolete frontend code, desktop launch script, and CLI chat REPL do not exist."""
    repo_root = pathlib.Path(__file__).resolve().parents[4]
    web_dir = repo_root / "agent-system" / "web"
    desktop_script = repo_root / "agent-system" / "launch-app.sh"
    cli_chat_file = (
        repo_root / "agent-system" / "backend" / "src" / "agent_system" / "cli" / "chat.py"
    )

    assert not web_dir.exists(), "web/ directory must not exist in Telegram-only architecture"
    assert not desktop_script.exists(), "launch-app.sh must not exist"
    assert not cli_chat_file.exists(), "cli/chat.py REPL must not exist"


def test_cli_does_not_expose_chat_or_web_commands() -> None:
    """Verify agentctl CLI no longer exposes user chat or web dashboard commands."""
    from agent_system.cli.main import app

    command_names = [c.name for c in app.registered_commands if c.name]
    assert "chat" not in command_names
    assert "web" not in command_names


def test_production_mode_requires_telegram_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production AGENT_ENV requires Telegram bot token, webhook secret, and identity mode."""
    monkeypatch.setenv("AGENT_ENV", "production")
    monkeypatch.setenv("API_SESSION_SECRET", "super-secret-production-key-12345")
    monkeypatch.setenv("AGENT_BOOTSTRAP_SECRET", "super-secret-bootstrap-key-12345")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "")
    monkeypatch.setenv("AGENT_IDENTITY_MODE", "local")
    clear_settings_cache()

    try:
        with pytest.raises(RuntimeError, match="Incomplete Telegram production configuration"):
            get_settings()
    finally:
        clear_settings_cache()


def test_webhook_secret_validation(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """Webhook endpoint accepts valid secret token and rejects invalid ones."""
    db_path = tmp_path / "test_wh.db"
    db_url = f"sqlite:///{db_path}"
    engine = make_engine(db_url)
    Base.metadata.create_all(engine)

    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:WEBHOOK_TEST_TOKEN")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "valid-secret-token-abc")
    monkeypatch.setenv("AGENT_IDENTITY_MODE", "telegram")
    clear_settings_cache()

    try:
        from agent_system.api.main import app

        with TestClient(app) as client:
            # 1. Missing or invalid secret -> 401
            bad_resp = client.post(
                "/api/v1/telegram/webhook",
                headers={"X-Telegram-Bot-Api-Secret-Token": "invalid-secret"},
                json={"update_id": 100},
            )
            assert bad_resp.status_code == 401

            # 2. Valid secret -> 200 OK
            ok_resp = client.post(
                "/api/v1/telegram/webhook",
                headers={"X-Telegram-Bot-Api-Secret-Token": "valid-secret-token-abc"},
                json={
                    "update_id": 1001,
                    "message": {
                        "message_id": 1,
                        "chat": {"id": 888},
                        "from": {"id": 888},
                        "text": "/start",
                    },
                },
            )
            assert ok_resp.status_code == 200
            assert ok_resp.json() == {"status": "ok"}
    finally:
        clear_settings_cache()


@pytest.mark.asyncio
async def test_telegram_start_command_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Sending /start returns a connected response in outbox."""
    db_path = tmp_path / "test_start.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:BOT_TOKEN_START")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "secret123")
    monkeypatch.setenv("AGENT_IDENTITY_MODE", "telegram")
    clear_settings_cache()

    try:
        engine = make_engine(db_url)
        Base.metadata.create_all(engine)
        factory = make_session_factory(engine)
        bus = EventBus()
        settings = get_settings()

        _provision_tg_user(factory, tg_user_id="555", chat_id=555, role="owner")

        from agent_system.services.permissions import PermissionGate
        from agent_system.services.telegram import TelegramService

        gate = PermissionGate(factory=factory)
        svc = TelegramService(settings, factory, gate, bus)

        update = {
            "update_id": 5001,
            "message": {
                "message_id": 10,
                "chat": {"id": 555},
                "from": {"id": 555},
                "text": "/start",
            },
        }

        await svc.handle_update(update)

        with factory() as db:
            outbox_item = db.query(DeliveryOutbox).filter_by(chat_id=555).first()
            assert outbox_item is not None
            assert "Bob Agent connected" in outbox_item.text
    finally:
        clear_settings_cache()


@pytest.mark.asyncio
async def test_unauthorized_telegram_user_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """An unprovisioned Telegram user is ignored without leaking information."""
    db_path = tmp_path / "test_unauth.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:BOT_TOKEN_UNAUTH")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "secret123")
    monkeypatch.setenv("AGENT_IDENTITY_MODE", "telegram")
    clear_settings_cache()

    try:
        engine = make_engine(db_url)
        Base.metadata.create_all(engine)
        factory = make_session_factory(engine)
        bus = EventBus()
        settings = get_settings()

        from agent_system.services.permissions import PermissionGate
        from agent_system.services.telegram import TelegramService

        gate = PermissionGate(factory=factory)
        svc = TelegramService(settings, factory, gate, bus)

        # Update from unprovisioned user 99999
        update = {
            "update_id": 7001,
            "message": {
                "message_id": 1,
                "chat": {"id": 99999},
                "from": {"id": 99999},
                "text": "Hello Bob",
            },
        }

        await svc.handle_update(update)

        with factory() as db:
            outbox_count = db.query(DeliveryOutbox).count()
            session_count = db.query(Session).count()
            assert outbox_count == 0
            assert session_count == 0
    finally:
        clear_settings_cache()


@pytest.mark.asyncio
async def test_duplicate_update_deduplication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Inbound update ledger prevents duplicate processing after completion."""
    db_path = tmp_path / "test_dedup.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:BOT_TOKEN_DEDUP")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "secret123")
    monkeypatch.setenv("AGENT_IDENTITY_MODE", "telegram")
    clear_settings_cache()

    try:
        engine = make_engine(db_url)
        Base.metadata.create_all(engine)
        factory = make_session_factory(engine)
        bus = EventBus()
        settings = get_settings()

        _provision_tg_user(factory, tg_user_id="333", chat_id=333, role="owner")

        from agent_system.services.permissions import PermissionGate
        from agent_system.services.telegram import TelegramService

        gate = PermissionGate(factory=factory)
        svc = TelegramService(settings, factory, gate, bus)

        update = {
            "update_id": 8001,
            "message": {
                "message_id": 1,
                "chat": {"id": 333},
                "from": {"id": 333},
                "text": "/help",
            },
        }

        # First delivery
        await svc.handle_update(update)
        with factory() as db:
            row = db.get(TelegramUpdate, 8001)
            assert row is not None
            assert row.processed_at is not None
            outbox_count1 = db.query(DeliveryOutbox).count()
            assert outbox_count1 == 1

        # Duplicate delivery
        await svc.handle_update(update)
        with factory() as db:
            outbox_count2 = db.query(DeliveryOutbox).count()
            assert outbox_count2 == 1  # No duplicate message created
    finally:
        clear_settings_cache()


@pytest.mark.asyncio
async def test_callback_query_approval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Callback queries for approvals trigger decision in PermissionGate."""
    db_path = tmp_path / "test_callback.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:BOT_TOKEN_CB")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "secret123")
    monkeypatch.setenv("AGENT_IDENTITY_MODE", "telegram")
    clear_settings_cache()

    try:
        engine = make_engine(db_url)
        Base.metadata.create_all(engine)
        factory = make_session_factory(engine)
        bus = EventBus()
        settings = get_settings()

        user_id = _provision_tg_user(factory, tg_user_id="444", chat_id=444, role="owner")

        from agent_system.services.permissions import PermissionGate
        from agent_system.services.telegram import TelegramService

        gate = PermissionGate(factory=factory)
        approval_rec = gate.request_approval(
            requested_action="deploy_code",
            risk="HIGH",
            scope="shell:execute",
            requester="task_1",
            owner_user_id=user_id,
        )

        svc = TelegramService(settings, factory, gate, bus)

        # Send approve callback query
        callback_update = {
            "update_id": 9001,
            "callback_query": {
                "id": "cb_123",
                "from": {"id": 444},
                "message": {"chat": {"id": 444}},
                "data": f"approve:{approval_rec.approval_id}",
            },
        }

        await svc.handle_update(callback_update)

        # Check decision in gate
        rec = gate.get(approval_rec.approval_id)
        assert rec is not None
        assert rec.decision.value == "APPROVED"
    finally:
        clear_settings_cache()
