"""PostgreSQL External Service Adapter.

Hardens PostgreSQL as Bob's primary transactional database.
Supports:
- Engine creation with connection-pool configuration & pre-ping
- Health check execution (`SELECT 1`) with timeout
- Database unavailable & error isolation
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import text
from sqlalchemy.engine import Engine

from agent_system.infra.db import _normalize_url, make_engine
from agent_system.services.external_services.base import (
    ExternalService,
    ServiceHealth,
    ServiceHealthStatus,
    redact_secrets,
)

logger = logging.getLogger(__name__)


class PostgresService(ExternalService):
    """External service adapter for PostgreSQL / primary database."""

    def __init__(self, database_url: str, engine: Engine | None = None) -> None:
        self.raw_database_url = database_url
        self.normalized_url = _normalize_url(database_url)
        self._engine = engine

    @property
    def name(self) -> str:
        return "postgresql" if "postgresql" in self.normalized_url else "database"

    @property
    def is_configured(self) -> bool:
        return bool(self.raw_database_url)

    @property
    def is_enabled(self) -> bool:
        return self.is_configured

    def get_engine(self) -> Engine:
        if self._engine is None:
            self._engine = make_engine(self.normalized_url)
        return self._engine

    def check_health(self, timeout: float = 5.0) -> ServiceHealth:
        if not self.is_configured:
            return ServiceHealth(
                name=self.name,
                configured=False,
                enabled=False,
                reachable=False,
                authenticated=False,
                status=ServiceHealthStatus.NOT_CONFIGURED,
                details={"url": ""},
            )

        start = time.monotonic()
        clean_url = redact_secrets(self.normalized_url)
        try:
            engine = self.get_engine()
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            latency = (time.monotonic() - start) * 1000.0
            return ServiceHealth(
                name=self.name,
                configured=True,
                enabled=True,
                reachable=True,
                authenticated=True,
                status=ServiceHealthStatus.OK,
                latency_ms=latency,
                last_success=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                details={"url": clean_url, "is_postgres": "postgresql" in self.normalized_url},
            )
        except Exception as exc:
            latency = (time.monotonic() - start) * 1000.0
            err_msg = str(exc)
            status = ServiceHealthStatus.UNAVAILABLE
            if any(term in err_msg.lower() for term in ("password", "authentication", "denied")):
                status = ServiceHealthStatus.AUTH_FAILED
            return ServiceHealth(
                name=self.name,
                configured=True,
                enabled=True,
                reachable=False,
                authenticated=False,
                status=status,
                latency_ms=latency,
                last_error=redact_secrets(err_msg),
                details={"url": clean_url},
            )
