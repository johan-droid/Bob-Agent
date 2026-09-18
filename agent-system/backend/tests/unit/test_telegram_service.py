"""Unit tests — Telegram gateway service (auth + command routing)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agent_system.config import Settings
from agent_system.infra.db import make_engine, make_session_factory, session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Base, TelegramUpdate
from agent_system.services.permissions import (
    ApprovalRequest,
    Decision,
    PermissionGate,
    Risk,
)
from agent_system.services.telegram import TelegramService


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "telegram_bot_token": "123456:TESTTOKEN",
        "telegram_allowed_chat_ids": "111,222",
        "telegram_webhook_secret": None,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _service(settings: Settings, gate: PermissionGate | None = None) -> TelegramService:
    return TelegramService(
        settings,
        None,
        gate or PermissionGate(),
        EventBus(),
    )


class TestAuth:
    def test_unauthorized_ignored_does_not_error(self) -> None:
        svc = _service(_settings())
        # An unlisted chat id must be a no-op (not authorized).
        assert svc._is_authorized(999) is False  # noqa: SLF001

    def test_authorized_chat(self) -> None:
        svc = _service(_settings())
        assert svc._is_authorized(111) is True
        assert svc._is_authorized(222) is True

    def test_default_settings_allow_nothing(self) -> None:
        svc = _service(_settings(telegram_allowed_chat_ids=""))
        assert svc._is_authorized(111) is False

    def test_parse_chat_ids_property(self) -> None:
        s = _settings(telegram_allowed_chat_ids=" 111 , 222 ,333")
        assert s.allowed_chat_ids == {111, 222, 333}


class TestConfigured:
    def test_not_configured_when_no_token(self) -> None:
        svc = _service(_settings(telegram_bot_token=None))
        assert svc.is_configured() is False

    def test_transport_polling_default(self) -> None:
        svc = _service(_settings())
        assert svc.transport == "polling"

    def test_transport_webhook_with_secret(self) -> None:
        svc = _service(_settings(telegram_webhook_secret="wsec"))
        assert svc.transport == "webhook"


class TestDecide:
    def test_decide_approves(self) -> None:
        gate = PermissionGate()
        rec = gate.request(
            ApprovalRequest(
                requested_action="fs.write",
                risk=Risk.MEDIUM,
                scope="file:write",
                requester="test",
            )
        )
        svc = _service(_settings(), gate=gate)

        async def run() -> None:
            await svc._decide(111, rec.approval_id, approve=True)  # noqa: SLF001

        asyncio.get_event_loop().run_until_complete(run())
        assert gate.get(rec.approval_id) is not None
        assert gate.get(rec.approval_id).decision == Decision.APPROVED  # type: ignore[union-attr]

    def test_decide_unknown_id(self) -> None:
        svc = _service(_settings())

        async def run() -> None:
            await svc._decide(111, "approval_nope", approve=True)  # noqa: SLF001

        asyncio.get_event_loop().run_until_complete(run())


class TestUpdateDispatch:
    @pytest.mark.asyncio
    async def test_callback_not_authorized_returns(self) -> None:
        svc = _service(_settings())
        updates = [
            {
                "update_id": 1,
                "callback_query": {
                    "id": "q1",
                    "from": {"id": 999},
                    "message": {"chat": {"id": 999}},
                    "data": "approve:abc",
                },
            }
        ]
        # Should not raise despite unauthorized.
        await svc.handle_update(updates[0])

    @pytest.mark.asyncio
    async def test_message_ignored_when_unauthed(self) -> None:
        svc = _service(_settings())
        await svc.handle_update(
            {
                "update_id": 2,
                "message": {
                    "chat": {"id": 999},
                    "text": "/status",
                },
            }
        )


class TestDurableLedger:
    """Crash-safe ingest ledger semantics (at-least-once work).

    INV-007: a retried delivery must be safe. An update is logged to
    ``telegram_updates`` before processing and marked COMPLETED
    (``processed_at``) only after processing succeeds. A redelivery whose row
    is still ``processed_at IS NULL`` (the crash window) must be re-processed,
    never dropped.
    """

    def _factory(self, tmp_path: Path) -> Any:
        engine = make_engine(f"sqlite:///{tmp_path / 'tg_ledger.db'}")
        Base.metadata.create_all(engine)
        return make_session_factory(engine)

    def _svc(self, factory: Any) -> TelegramService:
        settings = _settings()  # token set, allowlist {111, 222}
        return TelegramService(settings, factory, PermissionGate(), EventBus())

    def _update(self, update_id: int = 1) -> dict[str, Any]:
        return {
            "update_id": update_id,
            "message": {"chat": {"id": 111}, "from": {"id": 111}, "text": "/status"},
        }

    async def _deliver(self, svc: TelegramService, update: dict[str, Any]) -> int:
        """Deliver once; return how many dispatches happened."""
        calls: list[str] = []

        async def spy(principal: Any, chat_id: int, text: str) -> None:  # noqa: ARG001
            calls.append(text)

        original = svc._dispatch_command  # noqa: SLF001
        svc._dispatch_command = spy  # type: ignore[method-assign]  # noqa: SLF001
        try:
            await svc.handle_update(update)
        finally:
            svc._dispatch_command = original  # type: ignore[method-assign]  # noqa: SLF001
        return len(calls)

    @pytest.mark.asyncio
    async def test_fresh_update_is_processed_and_marked_completed(self, tmp_path: Path) -> None:
        factory = self._factory(tmp_path)
        svc = self._svc(factory)
        update = self._update(1)

        dispatches = await self._deliver(svc, update)

        assert dispatches == 1
        with session_scope(factory) as db:
            row = db.get(TelegramUpdate, 1)
            assert row is not None
            assert row.processed_at is not None

    @pytest.mark.asyncio
    async def test_crash_after_persist_redelivery_is_reclaimed(self, tmp_path: Path) -> None:
        """The mandatory P0 regression: ingest -> crash -> redeliver -> process.

        Simulate the on-disk state left by a crash that happened between the
        ingest INSERT and completing processing: a row whose ``processed_at``
        is still NULL. Delivering the same update again must process it exactly
        once (not skip it) and then mark it COMPLETED.
        """
        factory = self._factory(tmp_path)
        svc = self._svc(factory)
        update = self._update(1)
        # Crash window: first delivery ingested + persisted, then the process
        # died before any processing side effect or COMPLETED marker.
        with session_scope(factory) as db:
            db.add(TelegramUpdate(update_id=1, chat_id="111", payload_json=update))

        dispatches = await self._deliver(svc, update)

        assert dispatches == 1, "crash-window retry must be re-processed, not dropped"
        with session_scope(factory) as db:
            row = db.get(TelegramUpdate, 1)
            assert row is not None
            assert row.processed_at is not None, "retry must now be marked COMPLETED"

    @pytest.mark.asyncio
    async def test_completed_duplicate_delivery_is_skipped(self, tmp_path: Path) -> None:
        """A fully processed update (processed_at set) is at-most-once: a
        redelivery is skipped and cannot double-dispatch."""
        factory = self._factory(tmp_path)
        svc = self._svc(factory)
        update = self._update(1)

        first = await self._deliver(svc, update)
        assert first == 1
        # Redelivery AFTER COMPLETED must be dropped.
        second = await self._deliver(svc, update)

        assert second == 0
        with session_scope(factory) as db:
            row = db.get(TelegramUpdate, 1)
            assert row.processed_at is not None

    @pytest.mark.asyncio
    async def test_retry_layer_does_not_embed_processed_rows(self, tmp_path: Path) -> None:
        """After the reclaim path runs, the ledger must not double-mark or
        re-process on a second redelivery."""
        factory = self._factory(tmp_path)
        svc = self._svc(factory)
        update = self._update(7)
        with session_scope(factory) as db:
            db.add(TelegramUpdate(update_id=7, chat_id="111", payload_json=update))

        assert await self._deliver(svc, update) == 1  # reclaimed
        assert await self._deliver(svc, update) == 0  # now COMPLETED -> skipped


class TestSend:
    @pytest.mark.asyncio
    async def test_send_when_not_configured_is_noop(self) -> None:
        svc = _service(_settings(telegram_bot_token=None))
        # Should be a no-op without a token (no network call).
        await svc._send(111, "hello")  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_broadcast_to_allowed_builds_markup(self) -> None:
        svc = _service(_settings(telegram_allowed_chat_ids="111,222"))
        rec = PermissionGate().request(
            ApprovalRequest(
                requested_action="fs.write",
                risk=Risk.MEDIUM,
                scope="file:write",
                requester="test",
            )
        )
        # Replace the client transport so we don't hit the network.
        calls: list[dict[str, Any]] = []

        class FakeResp:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return {"ok": True}

        class FakeClient:
            async def post(self, *args: Any, **kwargs: Any) -> FakeResp:
                calls.append(kwargs)
                return FakeResp()

            async def aclose(self) -> None:
                return None

        svc._client = FakeClient()  # noqa: SLF001
        await svc.send_approval_request(rec)
        # Two approved chats -> two sendMessage calls with reply markup.
        assert len(calls) == 2
        assert all("inline_keyboard" in c["json"]["reply_markup"] for c in calls)
