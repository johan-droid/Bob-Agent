"""Unit tests — Telegram gateway service (auth + command routing)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_system.config import Settings
from agent_system.infra.event_bus import EventBus
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
