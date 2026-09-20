"""Unit tests for Bob's Telegram UX, conversation history, and internal ID stripping."""

from __future__ import annotations

from agent_system.config import Settings
from agent_system.infra.db import make_engine, make_session_factory
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Base, DeliveryOutbox
from agent_system.services.gateway import GatewayExecutor
from agent_system.services.telegram import TelegramService
from agent_system.services.telegram_presenter import (
    format_model_footer,
    load_chat_history,
    sanitize_telegram_message,
    save_chat_message,
)


def test_sanitize_telegram_message_strips_internal_ids() -> None:
    raw = (
        "Task accepted: session_01M2ZSBDK1XGTGJ8V8SXZ2MRQ7\n"
        "Task registered: task_01M2ZSBDK6ZDP95VX7NN9R7HSD\n"
        "Worker assigned: run_01M2ZSBDKB209179A01AGGS9N9\n"
        "Here is the answer you requested."
    )
    clean = sanitize_telegram_message(raw)
    assert "Task accepted" not in clean
    assert "session_" not in clean
    assert "task_" not in clean
    assert "Worker assigned" not in clean
    assert "Here is the answer you requested." in clean


def test_format_model_footer() -> None:
    footer = format_model_footer("groq", "llama-3.3-70b-versatile", 0.8)
    assert footer == "↳ groq · llama-3.3-70b-versatile · 0.8s"

    footer_no_latency = format_model_footer("opencode", "opencode/free-coding")
    assert footer_no_latency == "↳ opencode · free-coding"


def test_chat_history_persistence() -> None:
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    chat_id = 12345
    save_chat_message(factory, chat_id, "user", "hi")
    save_chat_message(factory, chat_id, "assistant", "Hey! 👋 What are we working on?")

    history = load_chat_history(factory, chat_id)
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "hi"
    assert history[1]["role"] == "assistant"
    assert history[1]["content"] == "Hey! 👋 What are we working on?"


def test_hi_conversational_path() -> None:
    settings = Settings(
        agent_identity_mode="local",
        telegram_allowed_chat_ids="100",
        telegram_bot_token="test:token",
    )
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    bus = EventBus()

    executor = GatewayExecutor(settings, factory, bus)

    # Ingest "hi" update
    update = {
        "update_id": 1,
        "message": {
            "message_id": 10,
            "chat": {"id": 100},
            "from": {"id": 100},
            "text": "hi",
        },
    }
    svc = TelegramService(settings, factory, None, bus)
    svc._log_update(update)

    executor.process_pending()

    # Check outbox delivery
    with factory() as db:
        rows = db.query(DeliveryOutbox).all()
        assert len(rows) > 0
        texts = [r.text for r in rows]
        assert any("↳" in t for t in texts)
        assert not any("Task accepted" in t for t in texts)
        assert not any("task_" in t for t in texts)


def test_normal_queries_do_not_create_task() -> None:
    from agent_system.infra.models import Session

    settings = Settings(
        agent_identity_mode="local",
        telegram_allowed_chat_ids="100",
        telegram_bot_token="test:token",
    )
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    bus = EventBus()

    executor = GatewayExecutor(settings, factory, bus)

    # Ingest "what did we discuss earlier?" update
    update = {
        "update_id": 2,
        "message": {
            "message_id": 11,
            "chat": {"id": 100},
            "from": {"id": 100},
            "text": "what did we discuss earlier?",
        },
    }
    svc = TelegramService(settings, factory, None, bus)
    svc._log_update(update)

    executor.process_pending()

    # Confirm no Session row was created for casual chat
    with factory() as db:
        sessions = db.query(Session).all()
        assert len(sessions) == 0
        outbox = db.query(DeliveryOutbox).all()
        assert len(outbox) == 2
        assert outbox[0].kind == "typing"
        assert outbox[1].kind == "notification"
        assert "Yep — I'll check that." not in outbox[1].text


def test_chat_mode_injects_soul() -> None:
    from typing import Any
    from unittest.mock import patch

    from agent_system.services.model_router import ModelRouter
    from agent_system.services.soul import load_soul

    _, soul_text = load_soul()
    assert soul_text != ""  # SOUL.md exists

    settings = Settings(
        agent_identity_mode="local",
        telegram_allowed_chat_ids="100",
        telegram_bot_token="test:token",
    )
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    bus = EventBus()

    executor = GatewayExecutor(settings, factory, bus)

    update = {
        "update_id": 3,
        "message": {
            "message_id": 12,
            "chat": {"id": 100},
            "from": {"id": 100},
            "text": "how are you?",
        },
    }
    svc = TelegramService(settings, factory, None, bus)
    svc._log_update(update)

    captured_soul: list[str | None] = []
    orig_invoke = ModelRouter.invoke

    def spy_invoke(self: Any, factory: Any, model_id: str, prompt: str, **kwargs: Any) -> Any:
        captured_soul.append(self.soul_text)
        return orig_invoke(self, factory, model_id, prompt, **kwargs)

    with patch.object(ModelRouter, "invoke", spy_invoke):
        executor.process_pending()

    assert len(captured_soul) == 1
    assert captured_soul[0] is not None
    assert "Bob" in captured_soul[0]


def test_chat_history_in_task_context() -> None:
    from agent_system.agents.react_agent import _with_memory
    from agent_system.infra.models import TelegramGatewayMessage

    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    chat_id = 100
    save_chat_message(factory, chat_id, "user", "I am working on Project Alpha.")
    save_chat_message(factory, chat_id, "assistant", "Sounds great!")

    session_id = "session_test_123"
    with factory() as db:
        db.add(
            TelegramGatewayMessage(
                id="tgm_123",
                chat_id=str(chat_id),
                session_id=session_id,
                processing_status="DISPATCHED",
            )
        )
        db.commit()

    task_prompt = _with_memory(Settings(), "Fix the README", factory=factory, session_id=session_id)
    assert "Project Alpha" in task_prompt
    assert "Recent Conversation History:" in task_prompt
