"""Unit tests for TelegramGatewayService adapter."""

from unittest.mock import MagicMock, patch

from agent_system.config import Settings
from agent_system.services.external_services.base import ServiceHealthStatus
from agent_system.services.external_services.telegram_service import TelegramGatewayService


def test_telegram_unconfigured():
    settings = Settings(telegram_bot_token="")
    service = TelegramGatewayService(settings)
    assert service.is_configured is False
    health = service.check_health()
    assert health.status == ServiceHealthStatus.NOT_CONFIGURED


def test_telegram_configured_health_check_success():
    settings = Settings(telegram_bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11", telegram_allowed_chat_ids="100,200")
    service = TelegramGatewayService(settings)

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True, "result": {"id": 123456, "username": "bob_test_bot"}}
        mock_client.get.return_value = mock_resp

        health = service.check_health()
        assert health.status == ServiceHealthStatus.OK
        assert health.reachable is True
        assert health.authenticated is True
        assert health.details["username"] == "bob_test_bot"
        assert 100 in health.details["allowed_chat_ids"]
        assert "ABC-DEF" not in str(health.to_dict())


def test_telegram_auth_failure():
    settings = Settings(telegram_bot_token="invalid_token")
    service = TelegramGatewayService(settings)

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_client.get.return_value = mock_resp

        health = service.check_health()
        assert health.status == ServiceHealthStatus.AUTH_FAILED
        assert health.authenticated is False
