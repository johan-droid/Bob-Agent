"""Execution Architecture Modes tests: CLOUD_INLINE_RUN=true vs CLOUD_INLINE_RUN=false."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_system.config import clear_settings_cache, get_settings
from agent_system.domain.events import utcnow
from agent_system.domain.ids import new_id
from agent_system.domain.lifecycles import AgentState
from agent_system.domain.tasks import TaskState
from agent_system.infra.db import make_engine, make_session_factory
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import (
    AgentLease,
    AgentRun,
    Base,
    DeliveryOutbox,
    Session,
    Task,
    TelegramAccount,
    TelegramUpdate,
    User,
)


def _provision_user(factory: Any, tg_user_id: str, chat_id: int, role: str = "owner") -> str:
    """Seed a Telegram identity -> active Bob user."""
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


def _ingest_update(factory: Any, update_id: int, chat_id: int, tg_user: str, text: str) -> None:
    """Insert raw update into ledger prior to executor processing."""
    with factory() as db:
        db.add(
            TelegramUpdate(
                update_id=update_id,
                chat_id=str(chat_id),
                account_id=tg_user,
                payload_json={
                    "update_id": update_id,
                    "message": {
                        "message_id": 100 + update_id,
                        "chat": {"id": chat_id},
                        "from": {"id": int(tg_user)},
                        "text": text,
                    },
                },
            )
        )
        db.commit()


def test_cloud_inline_run_true_without_redis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Mode A: CLOUD_INLINE_RUN=true without Redis.

    App boots healthily, Redis health is NOT_CONFIGURED, and Redis client is never created.
    """
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("CLOUD_INLINE_RUN", "true")
    monkeypatch.setenv("REDIS_URL", "")
    clear_settings_cache()

    try:
        from agent_system.api.main import app
        from agent_system.services.health import HealthRegistry

        settings = get_settings()
        assert settings.is_cloud_inline is True

        registry = HealthRegistry(settings)
        health = registry.check_all(timeout=1.0)
        assert health["status"] == "ok"
        assert health["services"]["redis"]["status"] == "NOT_CONFIGURED"

        with TestClient(app) as client:
            resp = client.get("/api/v1/health")
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok"}
    finally:
        clear_settings_cache()


def test_telegram_natural_language_goal_pipeline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Telegram NL Goal -> update ledger -> session -> task -> inline execution -> outbox."""
    db_path = tmp_path / "test_tg.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("CLOUD_INLINE_RUN", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1001")
    clear_settings_cache()

    try:
        engine = make_engine(db_url)
        Base.metadata.create_all(engine)
        factory = make_session_factory(engine)
        bus = EventBus()
        settings = get_settings()

        _provision_user(factory, tg_user_id="1001", chat_id=1001, role="owner")

        from agent_system.services.gateway import GatewayExecutor

        # 1. Update Ingest
        _ingest_update(
            factory,
            update_id=9001,
            chat_id=1001,
            tg_user="1001",
            text="Summarize today's release status",
        )

        # Confirm ledger record
        with factory() as db:
            ledger_row = db.get(TelegramUpdate, 9001)
            assert ledger_row is not None
            assert ledger_row.processed_at is None

        # 2. Process via GatewayExecutor
        executor = GatewayExecutor(settings, factory, bus)
        processed = executor.process_pending()
        assert processed == 1

        # 3. Verify session, task, and outbox results
        with factory() as db:
            session = db.query(Session).first()
            assert session is not None
            assert session.goal == "Summarize today's release status"

            task = db.query(Task).filter_by(session_id=session.id).first()
            assert task is not None
            assert task.state in (TaskState.SUCCEEDED.value, TaskState.FAILED.value)

            outbox_rows = db.query(DeliveryOutbox).all()
            assert len(outbox_rows) >= 1
            kinds = [r.kind for r in outbox_rows]
            assert "command_response" in kinds or "notification" in kinds
    finally:
        clear_settings_cache()


def test_restart_recovery_expired_lease_and_no_duplicate_execution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Queued tasks survive restart; expired RUNNING lease is recovered without duplication."""
    db_path = tmp_path / "test_rec.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("CLOUD_INLINE_RUN", "true")
    clear_settings_cache()

    try:
        engine = make_engine(db_url)
        Base.metadata.create_all(engine)
        factory = make_session_factory(engine)
        bus = EventBus()

        # Seed an orphaned running task with expired lease
        with factory() as db:
            session = Session(id="ses_rec1", goal="restart test", status="ACTIVE")
            db.add(session)
            db.commit()

            task = Task(
                id="tsk_rec1",
                session_id="ses_rec1",
                task_type="llm",
                title="test orphan task",
                input_json={"goal": "test orphan task"},
                state=TaskState.RUNNING.value,
                attempt=1,
            )
            db.add(task)
            db.commit()

            run = AgentRun(
                id="run_rec1",
                task_id="tsk_rec1",
                agent_type="llm",
                state=AgentState.RUNNING.value,
                worker_id="old-worker",
            )
            db.add(run)
            lease = AgentLease(
                agent_run_id="run_rec1",
                worker_id="old-worker",
                state=AgentState.RUNNING.value,
                heartbeat_at=utcnow() - timedelta(seconds=120),
                lease_expires_at=utcnow() - timedelta(seconds=60),
            )
            db.add(lease)
            db.commit()

        # Drive session (simulating restart recovery)
        from agent_system.services.cloud import drive_session

        summary = drive_session(factory, bus, "ses_rec1")
        assert summary["succeeded"] == 1
        assert summary["failed"] == 0

        # Verify task is SUCCEEDED and lease was cleaned up
        with factory() as db:
            task_row = db.get(Task, "tsk_rec1")
            assert task_row.state == TaskState.SUCCEEDED.value
            assert db.get(AgentLease, "run_rec1") is None

        # Re-running drive_session must be a no-op (no duplicate execution)
        again = drive_session(factory, bus, "ses_rec1")
        assert again["succeeded"] == 1
        assert again["total"] == 1
    finally:
        clear_settings_cache()


def test_mode_b_cloud_inline_false_requires_redis_and_worker_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mode B: CLOUD_INLINE_RUN=false needs REDIS_URL; worker refuses CLOUD_INLINE_RUN=true."""
    monkeypatch.setenv("CLOUD_INLINE_RUN", "true")
    clear_settings_cache()

    try:
        from agent_system.worker import main as worker_main

        err_msg = "Worker process cannot start when CLOUD_INLINE_RUN=true"
        with pytest.raises(RuntimeError, match=err_msg):
            worker_main()

        # Now test CLOUD_INLINE_RUN=false with empty REDIS_URL
        monkeypatch.setenv("CLOUD_INLINE_RUN", "false")
        monkeypatch.setenv("REDIS_URL", "")
        clear_settings_cache()

        with pytest.raises(RuntimeError, match="REDIS_URL is required when CLOUD_INLINE_RUN=false"):
            worker_main()
    finally:
        clear_settings_cache()


def test_redis_never_touched_by_inline_telegram_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Verify Redis is never imported/instantiated when Telegram inline execution runs."""
    db_path = tmp_path / "test_no_redis.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("CLOUD_INLINE_RUN", "true")
    monkeypatch.setenv("REDIS_URL", "redis://invalid-host-should-never-be-touched:6379/0")
    clear_settings_cache()

    try:
        engine = make_engine(db_url)
        Base.metadata.create_all(engine)
        factory = make_session_factory(engine)
        bus = EventBus()

        from agent_system.services.cloud import drive_session, ensure_session_tasks
        from agent_system.services.orchestrator import Supervisor

        supervisor = Supervisor(bus)
        session_id = supervisor.create_session(factory, "No redis goal")
        ensure_session_tasks(factory, bus, session_id)

        # Execution should succeed completely without throwing Redis connection error
        summary = drive_session(factory, bus, session_id)
        assert summary["succeeded"] == 1
    finally:
        clear_settings_cache()
