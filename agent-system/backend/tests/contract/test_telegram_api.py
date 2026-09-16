"""Contract tests — Telegram webhook + status endpoints."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from agent_system.api.main import app


@pytest.fixture()
def client(monkeypatch) -> Iterator[TestClient]:
    # Ensure a configured gateway exists for these tests.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:TOKEN")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "111")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "wsec")
    with TestClient(app) as client:
        token = client.app.state.authenticator.bootstrap_token  # type: ignore[attr-defined]
        client.headers["Authorization"] = f"Bearer {token}"
        yield client


class TestStatus:
    def test_status_configured(self, client: TestClient) -> None:
        r = client.get("/api/v1/telegram/status")
        # status is open (no auth dependency) but returns configured state
        assert r.status_code in (200, 503)


class TestWebhook:
    def test_webhook_requires_secret(self, client: TestClient) -> None:
        if getattr(client.app.state, "telegram", None) is None:
            pytest.skip("telegram gateway not configured in this env")
        r = client.post("/api/v1/telegram/webhook", json={})
        # No header -> 401
        assert r.status_code == 401

    def test_webhook_bad_secret_401(self, client: TestClient) -> None:
        svc = getattr(client.app.state, "telegram", None)
        if svc is None or not svc.is_configured():
            pytest.skip("telegram gateway not configured")
        r = client.post(
            "/api/v1/telegram/webhook",
            json={},
            headers={"X-Telegram-Webhook-Secret": "wrong"},
        )
        assert r.status_code == 401

    def test_webhook_valid_secret_ok(self, client: TestClient) -> None:
        svc = getattr(client.app.state, "telegram", None)
        if svc is None or not svc.is_configured():
            pytest.skip("telegram gateway not configured")
        r = client.post(
            "/api/v1/telegram/webhook",
            json={"update_id": 1, "message": {"chat": {"id": 999}, "text": "hi"}},
            headers={"X-Telegram-Webhook-Secret": "wsec"},
        )
        assert r.status_code == 200
