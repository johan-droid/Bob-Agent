"""Regression: a relayed failure and drive fallback sent the same apology twice."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from agent_system.config import Settings
from agent_system.domain.events import Event
from agent_system.infra.db import make_engine, make_session_factory
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Base, DeliveryOutbox, Task, TelegramUpdate

APOLOGY = "Sorry, I ran into an issue"


def _factory(tmp_path: Any, name: str = "dup.db") -> Any:
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _settings() -> Settings:
    return Settings(
        agent_env="dev",
        agent_identity_mode="telegram",
        telegram_allowed_user_ids="123456789",
        telegram_bot_token="test:bot_token",
        telegram_webhook_secret="test_secret",
        default_provider="echo",
        tools_require_approval=True,
    )


def _seed(factory: Any, settings: Settings) -> None:
    from agent_system.services.identity import IdentityService

    IdentityService(factory, settings).provision("123456789", display_name="Dup", chat_id=123456789)
    update = {
        "update_id": 9001,
        "message": {
            "message_id": 101,
            "chat": {"id": 123456789},
            "from": {"id": 123456789, "username": "testuser"},
            "text": "Audit release status and write report",
        },
    }
    with factory() as db:
        db.add(
            TelegramUpdate(
                update_id=9001,
                chat_id="123456789",
                account_id="123456789",
                payload_json=update,
            )
        )
        db.commit()


def _count_apologies(factory: Any) -> int:
    with factory() as db:
        rows = db.query(DeliveryOutbox).filter(DeliveryOutbox.text.like(f"%{APOLOGY}%")).all()
        return len(rows)


def test_relayed_failure_suppresses_drive_fallback(tmp_path: Any) -> None:
    """One apology, not two, when a drive emits task.failed and then raises."""
    import agent_system.services.cloud as cloud_mod
    import agent_system.services.gateway as gateway_mod
    import agent_system.services.outbox as outbox_mod
    import agent_system.services.telegram as telegram_mod

    factory = _factory(tmp_path)
    executor = gateway_mod.GatewayExecutor(_settings(), factory, EventBus())
    _seed(factory, executor._settings)
    executor.start()

    def _report_then_fail(
        drive_factory: Any, bus: EventBus, session_id: str, settings: Any = None
    ) -> None:
        with drive_factory() as db:
            task = db.query(Task).filter(Task.session_id == session_id).first()
            assert task is not None
            bus.emit(
                Event(
                    type="task.failed",
                    session_id=session_id,
                    task_id=task.id,
                    actor="llm",
                    payload={"error": "boom"},
                ),
                db,
            )
        raise RuntimeError("503")

    try:
        with (
            patch.object(telegram_mod, "send_chat_action_sync", lambda *a, **k: None),
            patch.object(cloud_mod, "drive_session", _report_then_fail),
            patch.object(
                gateway_mod.GatewayExecutor, "_bind_progress_message_id", lambda *a, **k: None
            ),
            patch.object(outbox_mod.Outbox, "drain", lambda *a, **k: 0),
        ):
            assert executor.process_pending(background=False) == 1
    finally:
        executor.stop()

    assert _count_apologies(factory) == 1
