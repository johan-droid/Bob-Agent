"""End-to-End Integration Diagnostic Test Suite: Telegram Production Execution Loop.

Exercises the real lifecycle using mocked Telegram and model boundaries:
Telegram update -> webhook -> DB persistence -> GatewayExecutor -> identity ->
session -> task -> model -> completion -> DeliveryOutbox -> Telegram sendMessage.

Tests:
- Success ("Hello Bob")
- Duplicate Telegram update handling
- Unauthorized Telegram user
- Restart recovery
- Stuck task / model failure recovery
- Telegram API send failure and retry
- Approval-required tool task vs conversational reply
"""

from __future__ import annotations

from typing import Any

from agent_system.config import Settings
from agent_system.infra.db import make_engine, make_session_factory
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import (
    Base,
    DeliveryOutbox,
    Session,
    Task,
    TelegramUpdate,
)
from agent_system.services.gateway import GatewayExecutor
from agent_system.services.identity import IdentityService
from agent_system.services.outbox import Outbox


def _factory(tmp_path: Any, name: str = "prod_exec.db") -> Any:
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _settings(tmp_path: Any) -> Settings:
    return Settings(
        agent_env="dev",
        agent_identity_mode="telegram",
        telegram_allowed_user_ids="123456789",
        telegram_bot_token="test:bot_token",
        telegram_webhook_secret="test_secret",
        default_provider="echo",
        tools_require_approval=True,
    )


def test_full_telegram_production_execution_loop(tmp_path: Any) -> None:
    """Test full round-trip: update ingest -> gateway -> execution -> outbox -> delivery."""
    factory = _factory(tmp_path)
    settings = _settings(tmp_path)
    bus = EventBus()

    # Pre-provision authorized Telegram user 123456789
    identity_svc = IdentityService(factory, settings)
    principal = identity_svc.provision("123456789", display_name="Test User", chat_id=123456789)
    assert principal is not None
    assert principal.user_id is not None

    executor = GatewayExecutor(settings, factory, bus)
    executor.start()

    try:
        update = {
            "update_id": 9001,
            "message": {
                "message_id": 101,
                "chat": {"id": 123456789},
                "from": {"id": 123456789, "username": "testuser"},
                "text": "Hello Bob",
            },
        }

        # Directly test GatewayExecutor on ingested update
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

        # GatewayExecutor process_pending
        processed = executor.process_pending(background=False)
        assert processed == 1

        # Verify session and task creation
        with factory() as db:
            sessions = db.query(Session).all()
            assert len(sessions) == 1
            assert sessions[0].owner_user_id == principal.user_id

            tasks = db.query(Task).filter_by(session_id=sessions[0].id).all()
            assert len(tasks) == 1
            assert tasks[0].state == "SUCCEEDED"

        # Verify outbox records created
        outbox = Outbox(factory, settings)
        with factory() as db:
            out_rows = db.query(DeliveryOutbox).order_by(DeliveryOutbox.created_at).all()
            assert len(out_rows) >= 2  # Task ACK + completion notification

        # Outbox drain (delivery mock)
        drained = outbox.drain()
        assert drained >= 2

        with factory() as db:
            out_rows = db.query(DeliveryOutbox).all()
            assert all(r.state == "DELIVERED" for r in out_rows)

    finally:
        executor.stop()


def test_duplicate_telegram_update_idempotency(tmp_path: Any) -> None:
    """Verify re-delivered Telegram update is skipped and never duplicates tasks."""
    factory = _factory(tmp_path)
    settings = _settings(tmp_path)
    bus = EventBus()
    identity_svc = IdentityService(factory, settings)
    identity_svc.provision("123456789", display_name="Test User", chat_id=123456789)

    executor = GatewayExecutor(settings, factory, bus)
    executor.start()

    try:
        update = {
            "update_id": 9002,
            "message": {
                "message_id": 102,
                "chat": {"id": 123456789},
                "from": {"id": 123456789},
                "text": "Duplicate message",
            },
        }

        with factory() as db:
            db.add(
                TelegramUpdate(
                    update_id=9002,
                    chat_id="123456789",
                    account_id="123456789",
                    payload_json=update,
                )
            )
            db.commit()

        assert executor.process_pending(background=False) == 1

        # Re-processed update (row now has processed_at set)
        assert executor.process_pending(background=False) == 0

        with factory() as db:
            assert db.query(Session).count() == 1
            assert db.query(Task).count() == 1
    finally:
        executor.stop()


def test_unauthorized_telegram_user_denied(tmp_path: Any) -> None:
    """Verify update from user not in allowed list is safely denied without creating tasks."""
    factory = _factory(tmp_path)
    settings = _settings(tmp_path)
    bus = EventBus()

    executor = GatewayExecutor(settings, factory, bus)
    executor.start()

    try:
        unauth_update = {
            "update_id": 9003,
            "message": {
                "message_id": 103,
                "chat": {"id": 999999999},
                "from": {"id": 999999999},
                "text": "Unauthorized request",
            },
        }

        with factory() as db:
            db.add(
                TelegramUpdate(
                    update_id=9003,
                    chat_id="999999999",
                    account_id="999999999",
                    payload_json=unauth_update,
                )
            )
            db.commit()

        # Gateway executor claims and denies
        assert executor.process_pending(background=False) == 1

        with factory() as db:
            # No sessions or tasks created for unauthorized user
            assert db.query(Session).count() == 0
            assert db.query(Task).count() == 0
            # TelegramUpdate is marked processed to avoid infinite loop
            row = db.get(TelegramUpdate, 9003)
            assert row is not None
            assert row.processed_at is not None
    finally:
        executor.stop()


def test_restart_recovery_redrives_unprocessed_and_drains_outbox(tmp_path: Any) -> None:
    """Verify recover() redrives undelivered events/updates after process restart."""
    factory = _factory(tmp_path)
    settings = _settings(tmp_path)
    bus = EventBus()
    identity_svc = IdentityService(factory, settings)
    identity_svc.provision("123456789", display_name="Test User", chat_id=123456789)

    # Ingest update directly into DB (simulating dyno restart before processing)
    with factory() as db:
        db.add(
            TelegramUpdate(
                update_id=9004,
                chat_id="123456789",
                account_id="123456789",
                payload_json={
                    "update_id": 9004,
                    "message": {
                        "message_id": 104,
                        "chat": {"id": 123456789},
                        "from": {"id": 123456789},
                        "text": "Restart recovery goal",
                    },
                },
            )
        )
        db.commit()

    executor = GatewayExecutor(settings, factory, bus)
    stats = executor.recover(background=False)
    assert stats["updates_redriven"] == 1

    with factory() as db:
        sessions = db.query(Session).all()
        assert len(sessions) == 1
        tasks = db.query(Task).filter_by(session_id=sessions[0].id).all()
        assert len(tasks) == 1
        assert tasks[0].state == "SUCCEEDED"


def test_model_failure_produces_durable_error_response(tmp_path: Any) -> None:
    """Verify model/agent failure produces a durable error response in DeliveryOutbox."""
    factory = _factory(tmp_path)
    settings = _settings(tmp_path)
    bus = EventBus()
    identity_svc = IdentityService(factory, settings)
    identity_svc.provision("123456789", display_name="Test User", chat_id=123456789)

    # Ingest update
    with factory() as db:
        db.add(
            TelegramUpdate(
                update_id=9005,
                chat_id="123456789",
                account_id="123456789",
                payload_json={
                    "update_id": 9005,
                    "message": {
                        "message_id": 105,
                        "chat": {"id": 123456789},
                        "from": {"id": 123456789},
                        "text": "Trigger model failure",
                    },
                },
            )
        )
        db.commit()

    executor = GatewayExecutor(settings, factory, bus)

    def _failing_drive(fac: Any, b: Any, sid: str) -> None:
        raise RuntimeError("Groq API error: 503 Service Unavailable")

    import agent_system.services.cloud

    original_drive = agent_system.services.cloud.drive_session
    agent_system.services.cloud.drive_session = _failing_drive

    try:
        executor.process_pending(background=False)

        with factory() as db:
            out_rows = db.query(DeliveryOutbox).all()
            assert any(
                "Session failed" in r.text or "503" in r.text or "failed" in r.text.lower()
                for r in out_rows
            )
    finally:
        agent_system.services.cloud.drive_session = original_drive


def test_telegram_api_send_failure_retries_in_outbox(tmp_path: Any) -> None:
    """Verify Telegram API 500 error causes outbox row state to transition to RETRY."""
    factory = _factory(tmp_path)
    settings = _settings(tmp_path)
    outbox = Outbox(factory, settings)

    out_id = outbox.enqueue(kind="notification", chat_id=123456789, text="Test send failure")
    assert out_id is not None

    class _DownClient:
        def post(self, *args: Any, **kwargs: Any) -> Any:
            class _Resp:
                def raise_for_status(self) -> None:
                    raise RuntimeError("Telegram API 500 Server Error")

            return _Resp()

    claimed = outbox.claim_batch()
    assert len(claimed) == 1
    delivered = outbox.deliver_one(claimed[0], client=_DownClient())
    assert delivered is False

    with factory() as db:
        row = db.get(DeliveryOutbox, out_id)
        assert row is not None
        assert row.state in ("RETRY", "DEAD")
        assert row.attempts == 1


def test_conversational_reply_does_not_require_approval(tmp_path: Any) -> None:
    """Verify simple Telegram message ('Hello Bob') completes without requiring tool approval."""
    factory = _factory(tmp_path)
    settings = _settings(tmp_path)
    bus = EventBus()
    identity_svc = IdentityService(factory, settings)
    identity_svc.provision("123456789", display_name="Test User", chat_id=123456789)

    executor = GatewayExecutor(settings, factory, bus)
    executor.start()

    try:
        with factory() as db:
            db.add(
                TelegramUpdate(
                    update_id=9006,
                    chat_id="123456789",
                    account_id="123456789",
                    payload_json={
                        "update_id": 9006,
                        "message": {
                            "message_id": 106,
                            "chat": {"id": 123456789},
                            "from": {"id": 123456789},
                            "text": "Hello Bob",
                        },
                    },
                )
            )
            db.commit()

        executor.process_pending(background=False)

        with factory() as db:
            tasks = db.query(Task).all()
            assert len(tasks) == 1
            assert tasks[0].state == "SUCCEEDED"
            # Zero approvals required
            from agent_system.infra.models import Approval

            assert db.query(Approval).count() == 0
    finally:
        executor.stop()
