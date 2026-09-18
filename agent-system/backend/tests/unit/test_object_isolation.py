"""P0#3: object-level multi-user isolation — adversarial matrix.

The product is local-first single-owner; the ownership boundary applies in the
shared (telegram / non-local identity) mode. This suite pins the enforced
invariants and the boundary's edges so the semantics cannot silently loosen:

- `PermissionGate.decide` refuses any access (even to already-decided records)
  when the decider is an identified user other than the record owner.
- The Telegram `/approve` + `/deny` path forwards the real principal identity,
  so the gate's ownership check fires there too.
- Telegram `/cancel` + `/retry` never affect a task owned by another user.
- Ownerless objects and principals without an identity keep legacy behaviour
  (the documented local hole, not a re-target this iteration).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_system.infra.db import make_engine, make_session_factory, session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Base, Session, Task
from agent_system.services.identity import IdentityMode, Principal, Role
from agent_system.services.permissions import (
    ApprovalRequest,
    Decision,
    PermissionGate,
    Risk,
)
from agent_system.services.permissions import (
    Policy as ApprovalPolicy,
)
from agent_system.services.telegram import TelegramService

REQ = ApprovalRequest(
    requested_action="write secret",
    risk=Risk.MEDIUM,
    scope="test:write",
    requester="agent",
)


class _DB:
    """Real per-test SQLite file DB so ownership reads go through the store."""

    def __init__(self, tmp_path: Any) -> None:
        self.engine = make_engine(f"sqlite:///{tmp_path / 'isolation.db'}")
        Base.metadata.create_all(self.engine)
        self.factory = make_session_factory(self.engine)


def _telegram_service(factory: Any, gate: PermissionGate) -> TelegramService:
    from agent_system.config import Settings

    settings = Settings(
        telegram_bot_token="123456:TEST_TOKEEN",
        telegram_allowed_chat_ids="111,222",
        telegram_identity_mode="telegram",
    )
    return TelegramService(settings, factory, gate, EventBus())


def test_gate_refuses_foreign_owner_even_after_decision() -> None:
    gate = PermissionGate(factory=None)
    record = gate.request(REQ.model_copy(update={"owner_user_id": "alice"}))
    gate.decide(
        record.approval_id,
        approve=True,
        policy=ApprovalPolicy.ALLOW_ALWAYS,
        decided_by_user_id="alice",
    )
    # Decided by someone else's owner -> refused even though non-PENDING.
    with pytest.raises(ValueError):
        gate.decide(
            record.approval_id,
            approve=True,
            policy=ApprovalPolicy.ALLOW_ALWAYS,
            decided_by_user_id="bob",
        )
    # Owner still fine.
    assert gate.get(record.approval_id).decision is Decision.APPROVED


def test_gate_ownerless_and_uidless_unaffected() -> None:
    gate = PermissionGate(factory=None)
    ownerless = gate.request(REQ.model_copy(update={"owner_user_id": None}))
    # No owner -> any identified decider may proceed (legacy behaviour).
    gate.decide(ownerless.approval_id, approve=True, decided_by_user_id="bob")
    assert gate.get(ownerless.approval_id).decision is Decision.APPROVED


def test_telegram_approve_owned_approval_identity_forwarded(tmp_path: Any) -> None:
    db = _DB(tmp_path)
    gate = PermissionGate(factory=db.factory)
    alice = gate.request(REQ.model_copy(update={"owner_user_id": "999"}))

    svc = _telegram_service(db.factory, gate)
    sent: list[str] = []

    async def run() -> None:
        svc._send = _capturing_send(sent)  # type: ignore[method-assign]
        await svc._cmd_approve_run(_principal("999"), 111, alice.approval_id, approve=True)
        await svc._cmd_approve_run(_principal("888"), 111, alice.approval_id, approve=True)

    asyncio.get_event_loop().run_until_complete(run())

    assert gate.get(alice.approval_id).decision is Decision.APPROVED
    assert any("-> APPROVED" in m for m in sent[:1])  # owner's verdict
    assert any("Decision failed" in m for m in sent[1:])  # foreigner refused


def test_ownerless_telegram_approval_still_decidable(tmp_path: Any) -> None:
    """NULL-owner approvals keep the current single-user behaviour."""
    db = _DB(tmp_path)
    gate = PermissionGate(factory=db.factory)
    rec = gate.request(REQ)
    assert rec.owner_user_id is None
    gate.decide(rec.approval_id, approve=True, decided_by_user_id="999")
    assert gate.get(rec.approval_id).decision is Decision.APPROVED


def _task(
    db: _DB,
    owner: str | None,
    task_id: str = "task_1",
    state: str = "FAILED",
) -> None:
    with session_scope(db.factory) as session:
        if session.get(Session, "ses_1") is None:
            session.add(Session(id="ses_1", goal="g"))
        session.add(
            Task(
                id=task_id,
                session_id="ses_1",
                task_type="code",
                title="t",
                owner_user_id=owner,
                state=state,
            )
        )


def test_telegram_retry_foreign_owned_task_refused(tmp_path: Any) -> None:
    db = _DB(tmp_path)
    _task(db, owner="999")

    svc = _telegram_service(db.factory, PermissionGate(factory=None))
    was_requeued: list[str] = []
    svc._send = _capturing_send(was_requeued)  # type: ignore[method-assign]

    async def run() -> None:
        await svc._cmd_retry(_principal("888"), 111, "task_1")
        await svc._cmd_retry(_principal("999"), 111, "task_1")

    asyncio.get_event_loop().run_until_complete(run())

    assert any("authored by you" in m for m in was_requeued[:1])
    assert any("Re-queued task" in m for m in was_requeued[1:])


def test_telegram_ownerless_and_uidless_control_unaffected(tmp_path: Any) -> None:
    db = _DB(tmp_path)
    _task(db, owner=None, task_id="task_ownerless")
    _task(db, owner="999", task_id="task_owned")
    svc = _telegram_service(db.factory, PermissionGate(factory=None))

    sent: list[str] = []
    svc._send = _capturing_send(sent)  # type: ignore[method-assign]
    run = asyncio.get_event_loop().run_until_complete

    # Ownerless task with an identified principal -> allowed (legacy).
    run(svc._cmd_retry(_principal("888"), 111, "task_ownerless"))
    assert any("Re-queued task" in m for m in sent)

    # UID-less principal (synthetic OPERATOR): ownership never enforced.
    assert svc._task_owned_by_another("task_owned", _principal_no_uid()) is False
    assert svc._task_owned_by_another("task_ownerless", _principal("888")) is False


def _capturing_send(sink: list[str]) -> Any:
    async def send(chat_id: Any, text: str, **_: Any) -> None:
        sink.append(text)

    return send


def _principal(user_id: str, role: Role = Role.MEMBER) -> Principal:
    return Principal(
        user_id=user_id,
        role=role,
        mode=IdentityMode.TELEGRAM,
        chat_id=111,
    )


def _principal_no_uid() -> Principal:
    return Principal(
        user_id=None,
        role=Role.MEMBER,
        mode=IdentityMode.TELEGRAM,
        chat_id=111,
    )


def _telegram_service(factory: Any, gate: PermissionGate) -> TelegramService:
    from agent_system.config import Settings

    settings = Settings(
        telegram_bot_token="123456:TEST_TOKEEN",
        telegram_allowed_chat_ids="111,222",
        telegram_identity_mode="telegram",
    )
    return TelegramService(settings, factory, gate, EventBus())
