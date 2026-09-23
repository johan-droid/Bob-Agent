"""Comprehensive E2E lifecycle tests for Telegram-first cloud agent harness.

Tests A-L:
Test A — webhook ingestion
Test B — identity resolution & auto-provisioning
Test C — gateway execution
Test D — agent execution
Test E — normal conversational request without approval deadlock
Test F — model failure
Test G — successful completion
Test H — outbox delivery
Test I — retry on transient failure
Test J — duplicate Telegram update
Test K — restart recovery
Test L — full E2E trace
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from agent_system.config import Settings
from agent_system.domain.events import Event
from agent_system.infra.db import make_engine, make_session_factory, session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Base, DeliveryOutbox, Session, Task, TelegramUpdate
from agent_system.services.gateway import GatewayExecutor
from agent_system.services.identity import IdentityService
from agent_system.services.outbox import Outbox
from agent_system.services.permissions import PermissionGate
from agent_system.services.telegram import TelegramService


@pytest.fixture
def factory():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


@pytest.fixture
def settings():
    return Settings(
        telegram_bot_token="test:token",
        telegram_webhook_secret="testsecret",
        telegram_allowed_user_ids="12345,67890",
        agent_identity_mode="telegram",
        outbox_max_attempts=3,
        outbox_backoff_base_seconds=0.1,
    )


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def gate(factory):
    return PermissionGate(factory=factory)


# Test A — webhook ingestion
@pytest.mark.asyncio
async def test_a_webhook_ingestion(factory, settings, bus, gate):
    svc = TelegramService(settings, factory, gate, bus)
    update = {
        "update_id": 101,
        "message": {
            "message_id": 1,
            "date": 1600000000,
            "chat": {"id": 999},
            "from": {"id": 12345, "first_name": "TestUser"},
            "text": "Hello Bob",
        },
    }
    await svc.handle_update(update)
    with session_scope(factory) as db:
        row = db.get(TelegramUpdate, 101)
        assert row is not None
        assert row.chat_id == "999"
        assert row.account_id == "12345"


# Test B — identity resolution & auto-provisioning
def test_b_identity_resolution(factory, settings):
    identity_svc = IdentityService(factory, settings)
    # 12345 is allowed in settings
    principal = identity_svc.resolve("12345", chat_id=999)
    assert principal is None  # Not provisioned yet

    # Auto-provisioning
    provisioned = identity_svc.provision("12345", "TestUser", chat_id=999)
    assert provisioned is not None
    assert provisioned.user_id is not None
    assert provisioned.telegram_user_id == "12345"

    resolved = identity_svc.resolve("12345", chat_id=999)
    assert resolved is not None
    assert resolved.user_id == provisioned.user_id


# Test C — gateway execution
def test_c_gateway_execution(factory, settings, bus):
    # Ingest update into DB first
    with session_scope(factory) as db:
        db.add(
            TelegramUpdate(
                update_id=102,
                account_id="12345",
                chat_id="999",
                payload_json={
                    "update_id": 102,
                    "message": {
                        "message_id": 2,
                        "chat": {"id": 999},
                        "from": {"id": 12345},
                        "text": "Analyze data",
                    },
                },
            )
        )

    executor = GatewayExecutor(settings, factory, bus)
    executor.start()
    with patch("agent_system.services.cloud.drive_session") as mock_drive:
        mock_drive.return_value = {"succeeded": 1, "failed": 0, "total": 1, "unfinished": 0}
        processed = executor.process_pending()
        assert processed == 1

    with session_scope(factory) as db:
        row = db.get(TelegramUpdate, 102)
        assert row.processed_at is not None
        session = db.query(Session).first()
        assert session is not None
        assert session.goal == "Analyze data"
    executor.stop()


# Test D — agent execution reaches drive_session
def test_d_agent_execution(factory, settings, bus):
    with session_scope(factory) as db:
        db.add(
            TelegramUpdate(
                update_id=103,
                account_id="12345",
                chat_id="999",
                payload_json={
                    "update_id": 103,
                    "message": {
                        "message_id": 3,
                        "chat": {"id": 999},
                        "from": {"id": 12345},
                        "text": "Execute task",
                    },
                },
            )
        )

    executor = GatewayExecutor(settings, factory, bus)
    executor.start()
    with patch("agent_system.services.cloud.drive_session") as mock_drive:
        mock_drive.return_value = {"succeeded": 1, "failed": 0, "total": 1, "unfinished": 0}
        executor.process_pending()
        assert mock_drive.called
        session_id_called = mock_drive.call_args[0][2]
        assert session_id_called is not None
    executor.stop()


# Test E — normal conversational request without approval deadlock
def test_e_normal_conversational_request(factory, settings, bus):
    from agent_system.agents.react_agent import llm_react_handler

    mock_router = MagicMock()
    mock_result = MagicMock()
    mock_result.ok = True
    mock_result.output = "Hello! How can I help you today?"
    mock_result.tokens_in = 10
    mock_result.tokens_out = 10
    mock_result.tokens_cached = 0
    mock_result.tool_calls = []
    mock_router.invoke_streaming.return_value = mock_result
    mock_router.invoke.return_value = mock_result
    mock_router.default_model = "echo"

    with patch("agent_system.agents.react_agent._build_router", return_value=mock_router):
        res = llm_react_handler(
            {"goal": "Hello Bob"},
            {
                "factory": factory,
                "session_id": "s1",
                "task_id": "t1",
                "bus": bus,
                "settings": settings,
            },
        )
        assert res["stopped"] == "done"
        assert res["output"] == "Hello! How can I help you today?"


# Test F — model failure produces durable failed task and user response
def test_f_model_failure(factory, settings, bus):
    with session_scope(factory) as db:
        db.add(
            TelegramUpdate(
                update_id=104,
                account_id="12345",
                chat_id="999",
                payload_json={
                    "update_id": 104,
                    "message": {
                        "message_id": 4,
                        "chat": {"id": 999},
                        "from": {"id": 12345},
                        "text": "Execute task do something that fails",
                    },
                },
            )
        )

    executor = GatewayExecutor(settings, factory, bus)
    executor.start()
    err = RuntimeError("Model provider unavailable")
    with patch("agent_system.services.cloud.drive_session", side_effect=err):
        executor.process_pending()

    with session_scope(factory) as db:
        rows = db.query(DeliveryOutbox).all()
        assert len(rows) >= 1
        fail_msg = [r for r in rows if "issue" in r.text or "failed" in r.text.lower()]
        assert len(fail_msg) == 1
    executor.stop()


# Test G — successful completion creates DeliveryOutbox entry
def test_g_successful_completion(factory, settings, bus):
    with session_scope(factory) as db:
        db.add(
            TelegramUpdate(
                update_id=105,
                account_id="12345",
                chat_id="999",
                payload_json={
                    "update_id": 105,
                    "message": {
                        "message_id": 5,
                        "chat": {"id": 999},
                        "from": {"id": 12345},
                        "text": "Simple task",
                    },
                },
            )
        )

    executor = GatewayExecutor(settings, factory, bus)
    executor.start()

    def fake_drive(f, b, session_id, settings=None):
        # Emit completed event
        with session_scope(factory) as db:
            task = db.query(Task).filter_by(session_id=session_id).first()
            task_id = task.id if task else "t1"
            bus.emit(
                Event(
                    type="task.completed",
                    session_id=session_id,
                    task_id=task_id,
                    payload={"output": "Task succeeded!"},
                ),
                db,
            )
        return {"succeeded": 1, "failed": 0, "total": 1, "unfinished": 0}

    with patch("agent_system.services.cloud.drive_session", side_effect=fake_drive):
        executor.process_pending()

    with session_scope(factory) as db:
        rows = db.query(DeliveryOutbox).filter(DeliveryOutbox.kind == "notification").all()
        assert len(rows) == 1
        assert "Task succeeded!" in rows[0].text
    executor.stop()


# Test H — outbox delivery
def test_h_outbox_delivery(factory, settings):
    outbox = Outbox(factory, settings)
    outbox_id = outbox.enqueue(kind="notification", chat_id=999, text="Hello world")
    assert outbox_id is not None

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_client.post.return_value = mock_resp

    count = outbox.drain(client=mock_client)
    assert count == 1
    with session_scope(factory) as db:
        row = db.get(DeliveryOutbox, outbox_id)
        assert row.state == "DELIVERED"


# Test I — retry transient failure
def test_i_retry_transient_failure(factory, settings):
    outbox = Outbox(factory, settings)
    outbox_id = outbox.enqueue(kind="notification", chat_id=999, text="Retry me")

    mock_client = MagicMock()
    status_err = httpx.HTTPStatusError(
        "500 Internal Server Error",
        request=MagicMock(),
        response=MagicMock(status_code=500),
    )
    mock_client.post.side_effect = status_err

    outbox.drain(client=mock_client)
    with session_scope(factory) as db:
        row = db.get(DeliveryOutbox, outbox_id)
        assert row.state == "RETRY"
        assert row.attempts == 1


# Test J — duplicate Telegram update
@pytest.mark.asyncio
async def test_j_duplicate_update(factory, settings, bus, gate):
    svc = TelegramService(settings, factory, gate, bus)
    update = {
        "update_id": 200,
        "message": {
            "message_id": 20,
            "chat": {"id": 999},
            "from": {"id": 12345},
            "text": "Dup check",
        },
    }

    # First ingest
    res1 = svc._log_update(update)
    assert res1 is True

    # Mark completed
    svc._mark_processed(200)

    # Second ingest of same update_id
    res2 = svc._log_update(update)
    assert res2 is False


# Test K — restart recovery
def test_k_restart_recovery(factory, settings, bus):
    with session_scope(factory) as db:
        db.add(
            TelegramUpdate(
                update_id=300,
                account_id="12345",
                chat_id="999",
                payload_json={
                    "update_id": 300,
                    "message": {
                        "message_id": 30,
                        "chat": {"id": 999},
                        "from": {"id": 12345},
                        "text": "Unprocessed from crash",
                    },
                },
            )
        )

    executor = GatewayExecutor(settings, factory, bus)
    executor.start()
    with patch("agent_system.services.cloud.drive_session") as mock_drive:
        mock_drive.return_value = {"succeeded": 1, "failed": 0, "total": 1, "unfinished": 0}
        res = executor.recover()
        assert res["updates_redriven"] == 1
    executor.stop()


# Test L — full E2E trace
@pytest.mark.asyncio
async def test_l_full_e2e_trace(factory, settings, bus, gate):
    svc = TelegramService(settings, factory, gate, bus)
    executor = GatewayExecutor(settings, factory, bus)
    executor.start()

    update = {
        "update_id": 400,
        "message": {
            "message_id": 40,
            "chat": {"id": 999},
            "from": {"id": 12345, "first_name": "E2EUser"},
            "text": "Do task Hello Bob E2E",
        },
    }

    def fake_drive(f, b, session_id, settings=None):
        with session_scope(factory) as db:
            task = db.query(Task).filter_by(session_id=session_id).first()
            task_id = task.id if task else "t_e2e"
            bus.emit(
                Event(
                    type="task.completed",
                    session_id=session_id,
                    task_id=task_id,
                    payload={"output": "Hello! I am Bob, your AI agent."},
                ),
                db,
            )
        return {"succeeded": 1, "failed": 0, "total": 1, "unfinished": 0}

    with patch("agent_system.services.cloud.drive_session", side_effect=fake_drive):
        # 1. Webhook receives update
        await svc.handle_update(update)

        # 2. Drive pending in background
        executor.process_pending()

    # Assert outbox received messages
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_client.post.return_value = mock_resp

    outbox = Outbox(factory, settings)
    drained = outbox.drain(client=mock_client)
    assert drained >= 1  # Task completed result

    # Verify mock call sent to chat 999
    sent_payloads = [call[1]["json"] for call in mock_client.post.call_args_list]
    assert any("Hello! I am Bob, your AI agent." in p.get("text", "") for p in sent_payloads)

    executor.stop()
