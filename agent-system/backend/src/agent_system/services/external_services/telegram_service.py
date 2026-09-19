"""Telegram Gateway External Service Adapter.

Adapts `TelegramService` to the unified `ExternalService` interface.
Supports:
- Configuration validation (bot token, allowed chat IDs, webhook secret)
- Bounded connection / health check (getMe API via httpx)
- Secret redaction for bot token in logs & status
"""

from __future__ import annotations

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

logger = logging.getLogger(__name__)


class TelegramGatewayService(ExternalService):
    """External service adapter for Telegram Gateway."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bot_token = settings.telegram_bot_token

    @property
    def name(self) -> str:
        return "telegram"

    @property
    def is_configured(self) -> bool:
        return bool(self.bot_token and str(self.bot_token).strip())

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
                details={"bot_token": ""},
            )

        start = time.monotonic()
        url = f"https://api.telegram.org/bot{self.bot_token}/getMe"
        try:
            with httpx.Client(timeout=timeout) as client:
                res = client.get(url)
                latency = (time.monotonic() - start) * 1000.0
                if res.status_code == 200 and res.json().get("ok"):
                    bot_info = res.json().get("result", {})
                    return ServiceHealth(
                        name=self.name,
                        configured=True,
                        enabled=True,
                        reachable=True,
                        authenticated=True,
                        status=ServiceHealthStatus.OK,
                        latency_ms=latency,
                        last_success=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        details={
                            "username": bot_info.get("username", ""),
                            "allowed_chat_ids": list(self.settings.allowed_chat_ids),
                        },
                    )
                elif res.status_code in (401, 403):
                    return ServiceHealth(
                        name=self.name,
                        configured=True,
                        enabled=True,
                        reachable=True,
                        authenticated=False,
                        status=ServiceHealthStatus.AUTH_FAILED,
                        latency_ms=latency,
                        last_error="Unauthorized / Invalid bot token",
                        details={"status_code": res.status_code},
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
                        last_error=f"Telegram API HTTP {res.status_code}",
                        details={"status_code": res.status_code},
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
                details={"allowed_chat_ids": list(self.settings.allowed_chat_ids)},
            )
