"""LLM Provider Service Adapter for Bob Agent.

Adapts the existing ModelRouter / ProviderHealthTracker to the unified `ExternalService` interface.
"""

from __future__ import annotations

import logging
import time

from agent_system.config import Settings
from agent_system.services.external_services.base import (
    ExternalService,
    ServiceHealth,
    ServiceHealthStatus,
    redact_secrets,
)
from agent_system.services.provider_health import ProviderHealthTracker
from agent_system.services.providers import PROVIDERS

logger = logging.getLogger(__name__)


class LLMProviderService(ExternalService):
    """External service adapter for a specific LLM provider (e.g. Groq, Gemini, OpenRouter)."""

    def __init__(
        self,
        provider_key: str,
        settings: Settings,
        tracker: ProviderHealthTracker | None = None,
    ) -> None:
        self.provider_key = provider_key.lower().strip()
        self.settings = settings
        self.tracker = tracker
        self.spec = PROVIDERS.get(self.provider_key)

    @property
    def name(self) -> str:
        return f"llm_{self.provider_key}"

    @property
    def is_configured(self) -> bool:
        if self.provider_key == "echo":
            return True
        key = self.settings.provider_api_key(self.provider_key)
        return bool(key and str(key).strip())

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
                details={"provider": self.provider_key},
            )

        if self.tracker is not None:
            status_obj = self.tracker.get(self.provider_key)
            status_enum = ServiceHealthStatus.OK
            if status_obj.health.value == "RATE_LIMITED":
                status_enum = ServiceHealthStatus.RATE_LIMITED
            elif status_obj.health.value in ("UNAVAILABLE", "DEGRADED"):
                status_enum = ServiceHealthStatus.UNAVAILABLE
            elif status_obj.health.value == "AUTH_FAILED":
                status_enum = ServiceHealthStatus.AUTH_FAILED
            elif status_obj.health.value == "DISABLED":
                status_enum = ServiceHealthStatus.DISABLED

            return ServiceHealth(
                name=self.name,
                configured=True,
                enabled=True,
                reachable=status_enum
                not in (ServiceHealthStatus.UNAVAILABLE, ServiceHealthStatus.DISABLED),
                authenticated=status_enum != ServiceHealthStatus.AUTH_FAILED,
                status=status_enum,
                last_error=redact_secrets(status_obj.last_error),
                details={
                    "provider": self.provider_key,
                    "consecutive_failures": status_obj.consecutive_failures,
                },
            )

        start = time.monotonic()
        return ServiceHealth(
            name=self.name,
            configured=True,
            enabled=True,
            reachable=True,
            authenticated=True,
            status=ServiceHealthStatus.OK,
            latency_ms=(time.monotonic() - start) * 1000.0,
            details={"provider": self.provider_key},
        )
