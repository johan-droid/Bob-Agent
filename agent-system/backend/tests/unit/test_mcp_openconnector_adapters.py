"""Unit tests for MCP and OpenConnector service adapters."""

from unittest.mock import MagicMock, patch

from agent_system.config import Settings
from agent_system.services.external_services.base import ServiceHealthStatus
from agent_system.services.external_services.mcp_openconnector import (
    MCPService,
    OpenConnectorService,
)


def test_mcp_unconfigured_and_configured():
    settings = Settings(mcp_servers="[]")
    mcp_service = MCPService(settings)
    assert mcp_service.is_configured is False
    assert mcp_service.check_health().status == ServiceHealthStatus.NOT_CONFIGURED

    settings_with_mcp = Settings(mcp_servers='[{"name": "test", "command": "echo"}]')
    mcp_service = MCPService(settings_with_mcp)
    assert mcp_service.is_configured is True
    health = mcp_service.check_health()
    assert health.status == ServiceHealthStatus.OK
    assert health.details["servers_count"] == 1


def test_openconnector_unconfigured_and_configured():
    settings = Settings(openconnector_base_url="")
    oc_service = OpenConnectorService(settings)
    assert oc_service.is_configured is False
    assert oc_service.check_health().status == ServiceHealthStatus.DISABLED

    settings_oc = Settings(openconnector_base_url="http://localhost:3000")
    oc_service = OpenConnectorService(settings_oc)
    assert oc_service.is_configured is True

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client.get.return_value = mock_resp

        health = oc_service.check_health()
        assert health.status == ServiceHealthStatus.OK
        assert health.reachable is True
