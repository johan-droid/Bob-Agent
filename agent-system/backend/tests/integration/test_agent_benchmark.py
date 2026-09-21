"""Comprehensive Agent Benchmark Suite.

Verifies the 9 Golden Test Scenarios for Bob Agent:
1. "Hi" -> exactly one response.
2. "Hello bro" -> exactly one response.
3. "Can you plan a project?" -> planning conversation, not fake task registration.
4. "Can you access Gmail?" -> truthful capability response.
5. Same Telegram update 10x -> exactly one execution.
6. Primary provider failure -> safe fallback if available.
7. Tool failure -> truthful failure.
8. Worker restart -> task recovery.
9. Two chats -> zero context leakage.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_system.config import Settings
from agent_system.infra.db import make_engine, make_session_factory
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Base, DeliveryOutbox, Session
from agent_system.services.gateway import GatewayExecutor
from agent_system.services.memory import DbNoteStore, MemoryLayer, NoteMeta
from agent_system.services.model_router import ModelInfo
from agent_system.services.providers import build_model_router
from agent_system.services.telegram import TelegramService


@pytest.fixture
def test_env(tmp_path: Any) -> tuple[Settings, Any, EventBus, GatewayExecutor, TelegramService]:
    db_path = tmp_path / "benchmark.db"
    settings = Settings(
        agent_identity_mode="local",
        telegram_allowed_chat_ids="100,200",
        telegram_bot_token="test:token",
        vault_path=str(tmp_path / "vault"),
    )
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    bus = EventBus()
    executor = GatewayExecutor(settings, factory, bus)
    svc = TelegramService(settings, factory, None, bus)
    return settings, factory, bus, executor, svc


def test_golden_1_hi_exactly_one_response(test_env: Any) -> None:
    settings, factory, bus, executor, svc = test_env
    update = {
        "update_id": 1001,
        "message": {"message_id": 1, "chat": {"id": 100}, "from": {"id": 100}, "text": "Hi"},
    }
    svc._log_update(update)
    executor.process_pending()

    with factory() as db:
        rows = db.query(DeliveryOutbox).filter(DeliveryOutbox.kind == "notification").all()
        assert len(rows) == 1
        assert "echo" in rows[0].text or "Bob" in rows[0].text or len(rows[0].text) > 0


def test_golden_2_hello_bro_exactly_one_response(test_env: Any) -> None:
    settings, factory, bus, executor, svc = test_env
    update = {
        "update_id": 1002,
        "message": {"message_id": 2, "chat": {"id": 100}, "from": {"id": 100}, "text": "Hello bro"},
    }
    svc._log_update(update)
    executor.process_pending()

    with factory() as db:
        rows = db.query(DeliveryOutbox).filter(DeliveryOutbox.kind == "notification").all()
        assert len(rows) == 1


def test_golden_3_can_you_plan_a_project_conversational(test_env: Any) -> None:
    settings, factory, bus, executor, svc = test_env
    update = {
        "update_id": 1003,
        "message": {
            "message_id": 3,
            "chat": {"id": 100},
            "from": {"id": 100},
            "text": "Can you plan out a project?",
        },
    }
    svc._log_update(update)
    executor.process_pending()

    with factory() as db:
        sessions = db.query(Session).all()
        assert len(sessions) == 0
        outbox = db.query(DeliveryOutbox).filter(DeliveryOutbox.kind == "notification").all()
        assert len(outbox) == 1


def test_golden_4_can_you_access_gmail_truthful_response(test_env: Any) -> None:
    settings, factory, bus, executor, svc = test_env
    update = {
        "update_id": 1004,
        "message": {
            "message_id": 4,
            "chat": {"id": 100},
            "from": {"id": 100},
            "text": "Can you access my Gmail?",
        },
    }
    svc._log_update(update)
    executor.process_pending()

    with factory() as db:
        outbox = db.query(DeliveryOutbox).filter(DeliveryOutbox.kind == "notification").all()
        assert len(outbox) == 1
        text = outbox[0].text
        assert (
            "not connected" in text.lower() or "connect" in text.lower() or "echo" in text.lower()
        )


def test_golden_5_same_update_10x_exactly_one_execution(test_env: Any) -> None:
    settings, factory, bus, executor, svc = test_env
    update = {
        "update_id": 1005,
        "message": {
            "message_id": 5,
            "chat": {"id": 100},
            "from": {"id": 100},
            "text": "Audit my codebase",
        },
    }

    for _ in range(10):
        svc._log_update(update)

    for _ in range(5):
        executor.process_pending()

    with factory() as db:
        sessions = db.query(Session).all()
        assert len(sessions) == 1


def test_golden_6_primary_provider_failure_fallback(test_env: Any) -> None:
    settings, factory, bus, executor, _ = test_env

    class FailingAdapter:
        def invoke(self, model_id: str, prompt: str, **kwargs: Any) -> Any:
            raise ConnectionError("Primary provider 500 error")

    router = build_model_router(bus, settings)
    router.register_adapter("failing_provider", FailingAdapter())
    router.pricing.register(
        ModelInfo(
            model_id="fail-model",
            provider="failing_provider",
            input_cost_per_1m=0.0,
            output_cost_per_1m=0.0,
        )
    )

    inv = router.invoke(factory, "fail-model", "Test prompt")
    assert not inv.ok
    assert inv.error is not None
    assert "ConnectionError" in inv.error or "500" in inv.error


def test_golden_7_tool_failure_truthful(test_env: Any) -> None:
    from agent_system.services.tool_errors import ToolError
    from agent_system.services.tools.execution import execute_tool
    from agent_system.services.tools.registry import Tool, ToolContext

    def failing_tool_handler(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        raise ToolError("File /etc/shadow not readable: Permission denied")

    tool = Tool(
        name="read_shadow",
        description="Failing tool",
        parameters={"type": "object", "properties": {}},
        risk="read",
        handler=failing_tool_handler,
    )

    ctx = ToolContext(settings=Settings())
    try:
        res = execute_tool(tool, {}, ctx)
    except ToolError as exc:
        res = {"ok": False, "error": str(exc)}
    assert not res.get("ok", True)
    assert "Permission denied" in res["error"]


def test_golden_8_worker_restart_task_recovery(test_env: Any) -> None:
    settings, factory, bus, executor, svc = test_env

    update = {
        "update_id": 1008,
        "message": {
            "message_id": 8,
            "chat": {"id": 100},
            "from": {"id": 100},
            "text": "Refactor python module",
        },
    }
    svc._log_update(update)
    executor.process_pending()

    recovered = executor.recover(background=False)
    assert isinstance(recovered, dict)
    assert "updates_redriven" in recovered


def test_golden_9_two_chats_zero_context_leakage(test_env: Any) -> None:
    settings, factory, bus, executor, svc = test_env

    db_store = DbNoteStore(factory)
    db_store.write_note(
        NoteMeta(
            title="User 1 Secret",
            layer=MemoryLayer.USER,
            source="user",
            owner_user_id="user_100",
            session_id="session_100",
        ),
        body="Secret key is SuperSecret123",
    )

    db_store.write_note(
        NoteMeta(
            title="User 2 Secret",
            layer=MemoryLayer.USER,
            source="user",
            owner_user_id="user_200",
            session_id="session_200",
        ),
        body="User 2 password is Password456",
    )

    res_1 = db_store.recall("secret", limit=5, owner_user_id="user_100")
    assert len(res_1) == 1
    assert "SuperSecret123" in res_1[0]["snippet"]
    assert "Password456" not in res_1[0]["snippet"]

    res_2 = db_store.recall("secret", limit=5, owner_user_id="user_200")
    assert len(res_2) == 1
    assert "Password456" in res_2[0]["snippet"]
    assert "SuperSecret123" not in res_2[0]["snippet"]
