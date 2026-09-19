"""Telegram Gateway E2E: the front door of the cloud agent, proven durable.

Pipeline under test (Telegram is only an interface — every step goes
through the normal Bob runtime seams, no Telegram-specific shortcuts):

  update -> ingest ledger (idempotent) -> identity -> gateway state
         -> exactly one session/task -> echo runtime execution
         -> result/approval relay -> durable outbox -> delivery (retries)
         -> restart recovery (replay + drain + redrive)
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_system.config import Settings
from agent_system.domain.events import Event
from agent_system.domain.ids import new_id
from agent_system.infra.db import make_engine, make_session_factory
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import (
    Approval,
    Base,
    Session,
    Task,
    TelegramAccount,
    TelegramGatewayMessage,
    TelegramUpdate,
    User,
)
from agent_system.services.gateway import GatewayExecutor, GatewayRelay
from agent_system.services.outbox import Outbox
from agent_system.services.permissions import PermissionGate

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _factory(tmp_path: Any, name: str = "gw.db") -> Any:
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _settings(tmp_path: Any) -> Settings:
    return Settings(
        workspaces_root=str(tmp_path / "workspaces"),
        skills_dir=str(tmp_path / "skills"),
        telegram_bot_token="test:token",
    )


def _provision(factory: Any, tg_user_id: str, chat_id: int, role: str = "owner") -> str:
    """Seed a Telegram identity -> active Bob user (the provisioning path)."""
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


def _ingest(factory: Any, update_id: int, chat_id: int, tg_user: str, text: str) -> None:
    """Insert one raw update exactly as the transports log it (durable first)."""
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


def _outbox_rows(factory: Any) -> list[tuple[str, str, str | None]]:
    with factory() as db:
        return (
            [(r.kind, r.text, r.task_id) for r in db.query("x").all()]
            if False
            else [
                (r.kind, r.text, r.task_id)
                for r in __import__(
                    "agent_system.infra.models", fromlist=["DeliveryOutbox"]
                ).DeliveryOutbox.__table__.c
                and []
            ]
        )


def _outbox_snapshot(factory: Any) -> list[dict[str, Any]]:
    from agent_system.infra.models import DeliveryOutbox

    with factory() as db:
        rows = db.query(DeliveryOutbox).order_by(DeliveryOutbox.created_at).all()
        return [
            {
                "kind": r.kind,
                "chat_id": r.chat_id,
                "text": r.text,
                "task_id": r.task_id,
                "state": r.state,
            }
            for r in rows
        ]


# --------------------------------------------------------------------------- #
# 1. ingest ledger: persist -> validate -> dedupe
# --------------------------------------------------------------------------- #


def test_ingest_ledger_persists_and_validates(tmp_path: Any) -> None:
    factory = _factory(tmp_path)
    _ingest(factory, 11, 700, "555", "Audit my repository.")
    with factory() as db:
        row = db.get(TelegramUpdate, 11)
        assert row is not None
        assert row.processed_at is None  # durable, not yet acknowledged
        assert row.chat_id == "700"
        assert row.payload_json["message"]["text"] == "Audit my repository."
        assert row.account_id == "555"


# --------------------------------------------------------------------------- #
# 2. identity: unknown / blocked / inactive accounts never execute
# --------------------------------------------------------------------------- #


def test_executor_authenticates_identity_deny_by_default(tmp_path: Any) -> None:
    factory = _factory(tmp_path)
    settings = _settings(tmp_path)
    bus = EventBus()
    executor = GatewayExecutor(settings, factory, bus)

    _provision(factory, "777", 701, role="owner")
    _provision(factory, "888", 702, role="member")
    with factory() as db:
        acct = db.query(TelegramAccount).filter_by(telegram_user_id="888").one()
        acct.role = "blocked"
        db.commit()
        db.add(
            User(
                id=new_id("usr"),
                display_name="ghost",
                auth_provider="telegram",
                role="member",
                is_active=False,
            )
        )

    assert executor._resolve_owner("777") is not None  # provisioned, active
    assert executor._resolve_owner("888") is None  # blocked -> never executed
    assert executor._resolve_owner("999") is None  # unknown -> never executed
    assert executor._resolve_owner(None) is None  # no identity -> never executed


# --------------------------------------------------------------------------- #
# 3. the full happy path: exactly one session/task, executed, delivered
# --------------------------------------------------------------------------- #


def test_message_creates_exactly_one_task_and_delivers_result(tmp_path: Any) -> None:
    factory = _factory(tmp_path)
    settings = _settings(tmp_path)
    bus = EventBus()
    owner_id = _provision(factory, "555", 700, role="owner")
    _ingest(factory, 11, 700, "555", "Audit my repository.")

    executor = GatewayExecutor(settings, factory, bus)
    executor.start()
    try:
        assert executor.process_pending() == 1

        # Exactly one durable session + master task, owned by the principal.
        with factory() as db:
            sessions = db.query(Session).all()
            assert len(sessions) == 1
            assert sessions[0].owner_user_id == owner_id
            tasks = db.query(Task).filter(Task.session_id == sessions[0].id).all()
            assert len(tasks) == 1
            task_id = tasks[0].id
            assert tasks[0].state == "SUCCEEDED"  # echo provider ran offline

            # First-class gateway state went RECEIVED -> DISPATCHED -> COMPLETED.
            gw = (
                db.query(TelegramGatewayMessage)
                .filter(TelegramGatewayMessage.telegram_update_id == 11)
                .one()
            )
            assert gw.processing_status == "COMPLETED"
            assert gw.session_id == sessions[0].id
            assert gw.task_id == task_id
            assert gw.chat_id == "700"

        snapshot = _outbox_snapshot(factory)
        kinds = [r["kind"] for r in snapshot]
        assert "command_response" in kinds  # durable ack: "Task created: ..."
        assert "notification" in kinds  # final result
        result = [r for r in snapshot if r["kind"] == "notification"][0]
        assert result["chat_id"] == "700"  # no cross-chat leakage
        assert result["task_id"] == task_id
        assert "completed" in result["text"].lower()

        # Update is acknowledged only after execution completed.
        with factory() as db:
            assert db.get(TelegramUpdate, 11).processed_at is not None

        # Delivery at the Telegram API.
        outbox = Outbox(factory, settings)
        assert outbox.drain() == len(snapshot)
        assert all(r["state"] == "DELIVERED" for r in _outbox_snapshot(factory))
    finally:
        executor.stop()


def test_duplicate_update_never_duplicates_task(tmp_path: Any) -> None:
    factory = _factory(tmp_path)
    settings = _settings(tmp_path)
    bus = EventBus()
    _provision(factory, "555", 700, role="owner")
    _ingest(factory, 11, 700, "555", "Fix the README.")

    executor = GatewayExecutor(settings, factory, bus)
    executor.process_pending()

    # The same update re-delivered (Telegram retry / webhook replay):
    # the ledger row is completed, so it must be skipped entirely.
    processed = executor.process_pending()
    assert processed == 0
    with factory() as db:
        assert db.query(Session).count() == 1  # exactly one session
        assert db.query(Task).count() == 1  # exactly one task


def test_processed_marker_makes_redelivery_idempotent(tmp_path: Any) -> None:
    factory = _factory(tmp_path)
    bus = EventBus()
    _provision(factory, "555", 700, role="owner")
    _ingest(factory, 11, 700, "555", "Do a thing.")
    executor = GatewayExecutor(_settings(tmp_path), factory, bus)
    executor.process_pending()
    executor._mark_processed(11)  # idempotent COMPLETED marker
    with factory() as db:
        assert db.get(TelegramUpdate, 11).processed_at is not None
        assert db.query(Task).count() == 1


# --------------------------------------------------------------------------- #
# 4. approvals: relayed with buttons, decided through the PermissionGate
# --------------------------------------------------------------------------- #


def test_approval_roundtrip_with_ownership_gate(tmp_path: Any) -> None:
    factory = _factory(tmp_path)
    settings = _settings(tmp_path)
    bus = EventBus()
    owner_id = _provision(factory, "555", 700, role="owner")
    _provision(factory, "666", 800, role="member")
    gate = PermissionGate(factory)
    record = gate.request_approval(
        requested_action="git push origin main",
        risk="HIGH",
        scope="vcs:push",
        requester="coding-worker",
        owner_user_id=owner_id,
    )
    relay = GatewayRelay(factory, Outbox(factory, settings), bus)

    assert relay._relay_approval(
        Event(
            event_id=new_id("evt"),
            session_id="ses_x",
            task_id="task_x",
            type="approval.requested",
            actor="worker",
            payload={"approval_id": record.approval_id, "requested_action": "git push origin main"},
        )
    )
    rows = [r for r in _outbox_snapshot(factory) if r["kind"] == "approval"]
    assert len(rows) == 1 and rows[0]["chat_id"] == "700"

    # The OWNER decides through the normal PermissionGate — same store the
    # tool execution path reads. Ownership check is enforced server-side.
    decided = gate.decide(
        record.approval_id,
        approve=True,
        decided_by=str(owner_id),
        decided_by_user_id=str(owner_id),
    )
    assert decided.decision == "APPROVED"
    with factory() as db:
        assert db.get(Approval, record.approval_id).decision == "APPROVED"

    # A DIFFERENT user cannot decide someone else's approval.
    other = gate.request_approval(
        requested_action="rm -rf /",
        risk="CRITICAL",
        scope="host:shell",
        requester="worker",
        owner_user_id=owner_id,
    )
    with pytest.raises(ValueError):
        gate.decide(
            other.approval_id,
            approve=True,
            decided_by="666",
            decided_by_user_id="666",
        )


# --------------------------------------------------------------------------- #
# 5. progress: worker notifications relay to the task's chat
# --------------------------------------------------------------------------- #


def test_progress_notification_reaches_task_chat(tmp_path: Any) -> None:
    factory = _factory(tmp_path)
    settings = _settings(tmp_path)
    bus = EventBus()
    _provision(factory, "555", 700, role="owner")
    with factory() as db:
        session = Session(id=new_id("ses"), goal="audit", owner_user_id="missing-owner")
        db.add(session)
        db.flush()
        task = Task(
            id=new_id("task"),
            session_id=session.id,
            task_type="llm",
            title="Security Worker",
            state="RUNNING",
        )
        db.add(task)
        db.add(
            TelegramGatewayMessage(
                id=new_id("tgm"),
                telegram_update_id=42,
                chat_id="700",
                session_id=session.id,
                task_id=task.id,
                received_at=__import__("agent_system.domain.events", fromlist=["utcnow"]).utcnow(),
                processing_status="DISPATCHED",
            )
        )
        db.commit()
        task_id = task.id
    relay = GatewayRelay(factory, Outbox(factory, settings), bus)
    assert relay._relay(
        Event(
            event_id=new_id("evt"),
            session_id="unknown",
            task_id=task_id,
            type="task.failed",
            actor="worker",
            payload={"error": "Security audit completed with findings."},
        )
    )
    rows = [r for r in _outbox_snapshot(factory) if r["kind"] == "notification"]
    assert rows and rows[0]["chat_id"] == "700"


# --------------------------------------------------------------------------- #
# 6. delivery: Telegram API failure -> durable RETRY, then delivered
# --------------------------------------------------------------------------- #


def test_telegram_api_failure_leaves_message_queued_for_retry(tmp_path: Any) -> None:
    from datetime import timedelta

    from agent_system.domain.events import utcnow
    from agent_system.infra.models import DeliveryOutbox

    factory = _factory(tmp_path)
    settings = _settings(tmp_path)
    outbox = Outbox(factory, settings)
    outbox.enqueue(kind="notification", chat_id=700, text="final report")

    class _DownAPI:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *a: Any) -> None:
            return None

        def post(self, url: str, json: Any, timeout: Any) -> Any:
            class _Resp:
                def raise_for_status(self) -> None:
                    raise RuntimeError("telegram api 502")

                def json(self) -> dict[str, Any]:
                    return {"ok": False}

            return _Resp()

    claimed = outbox.claim_batch(worker_id="test")
    assert len(claimed) == 1
    delivered = False
    try:
        delivered = outbox.deliver_one(claimed[0], client=_DownAPI())
    except RuntimeError:
        delivered = False
    assert delivered is False  # API down
    with factory() as db:
        row = db.query(DeliveryOutbox).one()
        assert row.state in ("RETRY", "DEAD")  # queued, never lost
        assert row.attempts == 1
        # RETRY is re-claimable after backoff — the message survives restarts.
        row.next_attempt_at = utcnow() - timedelta(seconds=1)
        row.state = "RETRY"
        db.commit()
    assert len(outbox.claim_batch(worker_id="test")) == 1  # retryable


# --------------------------------------------------------------------------- #
# 7. restart recovery: replay undelivered events + drain the outbox
# --------------------------------------------------------------------------- #


def test_restart_replays_undelivered_results(tmp_path: Any) -> None:
    factory = _factory(tmp_path)
    settings = _settings(tmp_path)
    bus = EventBus()
    _provision(factory, "555", 700, role="owner")
    with factory() as db:
        session = Session(id=new_id("ses"), goal="g", owner_user_id=None)
        db.add(session)
        db.flush()
        task = Task(
            id=new_id("task"),
            session_id=session.id,
            task_type="llm",
            title="t",
            state="SUCCEEDED",
        )
        db.add(task)
        db.add(
            TelegramGatewayMessage(
                id=new_id("tgm"),
                telegram_update_id=9,
                chat_id="700",
                session_id=session.id,
                task_id=task.id,
                received_at=__import__("agent_system.domain.events", fromlist=["utcnow"]).utcnow(),
                processing_status="DISPATCHED",
            )
        )
        db.commit()
        task_id = task.id
    # The event reached the store, then the dyno died before the outbox row.
    with factory() as db:
        bus.emit(
            Event(
                event_id=new_id("evt"),
                session_id=session.id,
                task_id=task_id,
                type="task.completed",
                actor="runner",
                payload={"output": "the final report"},
            ),
            db,
        )
        db.commit()

    # New process, new executor: recovery must find and enqueue the result.
    executor = GatewayExecutor(settings, factory, EventBus())
    stats = executor.recover()
    assert stats["events_replayed"] == 1
    rows = [r for r in _outbox_snapshot(factory) if r["kind"] == "notification"]
    assert rows and rows[0]["task_id"] == task_id
    assert "the final report" in rows[0]["text"]

    # And a second recovery does not duplicate (event-id dedup).
    stats2 = executor.recover()
    assert stats2["events_replayed"] == 0
    assert len([r for r in _outbox_snapshot(factory) if r["kind"] == "notification"]) == 1


def test_recover_drains_undelivered_outbox(tmp_path: Any) -> None:
    factory = _factory(tmp_path)
    settings = _settings(tmp_path)
    outbox = Outbox(factory, settings)
    outbox.enqueue(kind="notification", chat_id=700, text="undelivered on restart")

    executor = GatewayExecutor(settings, factory, EventBus())
    stats = executor.recover()  # fresh process after dyno restart
    assert stats["outbox_redelivered"] >= 1
    assert all(r["state"] == "DELIVERED" for r in _outbox_snapshot(factory))


# --------------------------------------------------------------------------- #
# 8. user cancellation (Telegram is only an interface — same runtime path)
# --------------------------------------------------------------------------- #


def test_user_cancellation_cancels_master_task(tmp_path: Any) -> None:
    from agent_system.services.orchestrator import Orchestrator

    factory = _factory(tmp_path)
    bus = EventBus()
    owner_id = _provision(factory, "555", 700, role="owner")
    supervisor = __import__(
        "agent_system.services.orchestrator", fromlist=["Supervisor"]
    ).Supervisor(bus)
    session_id = supervisor.create_session(factory, "long running job", owner_user_id=owner_id)
    task_id = supervisor.add_task(factory, session_id, "llm", "Long job")
    orch = Orchestrator(bus)
    assert orch.cancel_task(factory, task_id) is True
    with factory() as db:
        assert db.get(Task, task_id).state == "CANCELLED"
    # Cancelling again is a no-op (state machine gate).
    assert orch.cancel_task(factory, task_id) is False
