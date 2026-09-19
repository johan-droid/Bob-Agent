"""Universal Service Health System for Bob Agent.

Consolidates status across all external services:
- PostgreSQL
- Redis
- MongoDB
- Object Storage (Local / S3)
- LLMs (Groq, Gemini, OpenRouter, OpenAI, etc.)
- Telegram
- MCP
- OpenConnector
- Search (DuckDuckGo)

Exposes consolidated health output with secret redaction and bounded timeouts.
"""

from __future__ import annotations

import concurrent.futures
import logging
from typing import Any

from agent_system.config import Settings, get_settings
from agent_system.services.external_services.base import (
    ExternalService,
    ServiceHealth,
    ServiceHealthStatus,
)
from agent_system.services.external_services.llm_service import LLMProviderService
from agent_system.services.external_services.mcp_openconnector import (
    MCPService,
    OpenConnectorService,
)
from agent_system.services.external_services.mongodb import MongoDBAdapter
from agent_system.services.external_services.postgres import PostgresService
from agent_system.services.external_services.redis_service import RedisService
from agent_system.services.external_services.search import DuckDuckGoSearchProvider
from agent_system.services.external_services.storage import (
    LocalStorageProvider,
    S3CompatibleStorageProvider,
)
from agent_system.services.external_services.telegram_service import TelegramGatewayService
from agent_system.services.provider_health import ProviderHealthTracker

logger = logging.getLogger(__name__)


class HealthRegistry:
    """Consolidated registry for checking all registered external services."""

    def __init__(
        self,
        settings: Settings | None = None,
        provider_tracker: ProviderHealthTracker | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.provider_tracker = provider_tracker
        self._services: list[ExternalService] = []
        self._initialize_default_services()

    def _initialize_default_services(self) -> None:
        # Core & Databases
        self.register(PostgresService(self.settings.database_url))
        self.register(RedisService(self.settings))
        self.register(MongoDBAdapter(self.settings))

        # Storage
        if self.settings.storage_provider.lower() == "s3":
            self.register(S3CompatibleStorageProvider(self.settings))
        else:
            self.register(LocalStorageProvider(self.settings))

        # LLMs
        for p_key in ("groq", "gemini", "openrouter", "openai", "anthropic"):
            self.register(LLMProviderService(p_key, self.settings, tracker=self.provider_tracker))

        # Gateways & Integrations
        self.register(TelegramGatewayService(self.settings))
        self.register(MCPService(self.settings))
        self.register(OpenConnectorService(self.settings))
        self.register(DuckDuckGoSearchProvider(self.settings))

    def register(self, service: ExternalService) -> None:
        self._services.append(service)

    def check_all(self, timeout: float = 3.0) -> dict[str, Any]:
        """Check health of all services concurrently with bounded timeout."""
        results: dict[str, Any] = {}
        all_ok = True

        def _check(service: ExternalService) -> ServiceHealth:
            try:
                return service.check_health(timeout=timeout)
            except Exception as exc:
                return ServiceHealth(
                    name=service.name,
                    configured=service.is_configured,
                    enabled=service.is_enabled,
                    reachable=False,
                    authenticated=False,
                    status=ServiceHealthStatus.UNAVAILABLE,
                    last_error=str(exc),
                )

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(self._services) or 1) as executor:
            future_to_service = {
                executor.submit(_check, s): s for s in self._services
            }
            for future in concurrent.futures.as_completed(future_to_service):
                service = future_to_service[future]
                try:
                    sh = future.result(timeout=timeout + 1.0)
                    results[sh.name] = sh.to_dict()
                    if sh.enabled and sh.status in (
                        ServiceHealthStatus.UNAVAILABLE,
                        ServiceHealthStatus.AUTH_FAILED,
                    ):
                        all_ok = False
                except Exception as exc:
                    results[service.name] = {
                        "name": service.name,
                        "configured": service.is_configured,
                        "enabled": service.is_enabled,
                        "reachable": False,
                        "authenticated": False,
                        "status": ServiceHealthStatus.UNAVAILABLE.value,
                        "last_error": f"Health check timed out: {exc}",
                    }
                    all_ok = False

        status_str = "ok" if all_ok else "degraded"
        return {
            "status": status_str,
            "services": results,
            "checks": {k: v.get("status") in ("OK", "DISABLED", "NOT_CONFIGURED") for k, v in results.items()},
        }
