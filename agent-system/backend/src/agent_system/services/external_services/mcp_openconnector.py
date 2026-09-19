"""MCP and OpenConnector External Service Adapters for Bob Agent."""

from __future__ import annotations

import json
import logging
import time

import httpx

from agent_system.config import Settings
from agent_system.services.external_services.base import (
    ExternalService,
    ServiceHealth,
    ServiceHealthStatus,
    redact_secrets,
)
from agent_system.services.openconnector import is_configured as oc_is_configured

logger = logging.getLogger(__name__)


class MCPService(ExternalService):
    """External service adapter for configured MCP servers (stdio & HTTP)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def name(self) -> str:
        return "mcp"

    @property
    def is_configured(self) -> bool:
        raw = self.settings.mcp_servers
        if not raw or raw == "[]":
            return False
        try:
            parsed = json.loads(raw)
            return isinstance(parsed, list) and len(parsed) > 0
        except Exception:
            return False

    @property
    def is_enabled(self) -> bool:
        return self.is_configured

    def check_health(self, timeout: float = 5.0) -> ServiceHealth:
        if not self.is_configured:
            return ServiceHealth(
                name=self.name,
                configured=False,
                enabled=False,
                reachable=False,
                authenticated=False,
                status=ServiceHealthStatus.NOT_CONFIGURED,
                details={"servers_count": 0},
            )

        try:
            servers = json.loads(self.settings.mcp_servers)
            return ServiceHealth(
                name=self.name,
                configured=True,
                enabled=True,
                reachable=True,
                authenticated=True,
                status=ServiceHealthStatus.OK,
                details={"servers_count": len(servers)},
            )
        except Exception as exc:
            return ServiceHealth(
                name=self.name,
                configured=True,
                enabled=True,
                reachable=False,
                authenticated=False,
                status=ServiceHealthStatus.UNAVAILABLE,
                last_error=redact_secrets(str(exc)),
            )


class OpenConnectorService(ExternalService):
    """External service adapter for OpenConnector SaaS gateway."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def name(self) -> str:
        return "openconnector"

    @property
    def is_configured(self) -> bool:
        return oc_is_configured(self.settings)

    @property
    def is_enabled(self) -> bool:
        return self.is_configured

    def check_health(self, timeout: float = 5.0) -> ServiceHealth:
        if not self.is_configured:
            return ServiceHealth(
                name=self.name,
                configured=False,
                enabled=False,
                reachable=False,
                authenticated=False,
                status=ServiceHealthStatus.DISABLED,
                details={"base_url": ""},
            )

        start = time.monotonic()
        base_url = self.settings.openconnector_base_url.rstrip("/")
        clean_url = redact_secrets(base_url)

        try:
            with httpx.Client(timeout=timeout) as client:
                res = client.get(f"{base_url}/health")
                latency = (time.monotonic() - start) * 1000.0
                if res.status_code == 200:
                    return ServiceHealth(
                        name=self.name,
                        configured=True,
                        enabled=True,
                        reachable=True,
                        authenticated=True,
                        status=ServiceHealthStatus.OK,
                        latency_ms=latency,
                        last_success=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        details={"base_url": clean_url},
                    )
                else:
                    return ServiceHealth(
                        name=self.name,
                        configured=True,
                        enabled=True,
                        reachable=False,
                        authenticated=False,
                        status=ServiceHealthStatus.UNAVAILABLE,
                        latency_ms=latency,
                        last_error=f"OpenConnector returned HTTP {res.status_code}",
                        details={"base_url": clean_url},
                    )
        except Exception as exc:
            latency = (time.monotonic() - start) * 1000.0
            return ServiceHealth(
                name=self.name,
                configured=True,
                enabled=True,
                reachable=False,
                authenticated=False,
                status=ServiceHealthStatus.UNAVAILABLE,
                latency_ms=latency,
                last_error=redact_secrets(str(exc)),
                details={"base_url": clean_url},
            )
