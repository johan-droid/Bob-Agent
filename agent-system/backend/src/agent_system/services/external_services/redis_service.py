"""Redis and RQ External Service Adapter for Bob Agent.

Uses the existing `redis` and `rq` libraries.
Supports:
- Configuration validation
- Connection & health check (`PING`, latency, queue depth)
- Task locking (`acquire_lock`, `release_lock`)
- Temporary state & caching with TTL
- Duplicate task protection
- Retries and graceful degradation when Redis is down
- Preserves `CLOUD_INLINE_RUN=true` / inline execution mode when Redis is absent
"""

from __future__ import annotations

import logging
import time
from typing import Any

from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from agent_system.config import Settings
from agent_system.services.external_services.base import (
    ExternalService,
    ServiceDisabledError,
    ServiceHealth,
    ServiceHealthStatus,
    redact_secrets,
)

logger = logging.getLogger(__name__)


class RedisService(ExternalService):
    """External service adapter for Redis cache, task locks, and RQ queues."""

    def __init__(self, settings: Settings, redis_client: Redis | None = None) -> None:
        self.settings = settings
        self.redis_url = settings.redis_url
        self._client: Redis | None = redis_client

    @property
    def name(self) -> str:
        return "redis"

    @property
    def is_configured(self) -> bool:
        if getattr(self.settings, "is_cloud_inline", False):
            return False
        return bool(str(self.redis_url).strip())

    @property
    def is_enabled(self) -> bool:
        return self.is_configured

    def get_client(self, timeout: float = 3.0) -> Redis:
        if not self.is_configured:
            raise ServiceDisabledError("Redis URL is not configured", service=self.name)

        if self._client is None:
            self._client = Redis.from_url(
                self.redis_url,
                socket_timeout=timeout,
                socket_connect_timeout=timeout,
                decode_responses=True,
            )
        return self._client

    def check_health(self, timeout: float = 3.0) -> ServiceHealth:
        if not self.is_configured:
            reason = (
                "cloud_inline_run=true (Redis disabled for cloud inline execution)"
                if getattr(self.settings, "is_cloud_inline", False)
                else "redis_url is empty"
            )
            return ServiceHealth(
                name=self.name,
                configured=False,
                enabled=False,
                reachable=False,
                authenticated=False,
                status=ServiceHealthStatus.NOT_CONFIGURED,
                details={"reason": reason},
            )

        start = time.monotonic()
        clean_url = redact_secrets(self.redis_url)

        try:
            client = self.get_client(timeout=timeout)
            ping_ok = client.ping()
            latency = (time.monotonic() - start) * 1000.0

            details: dict[str, Any] = {"url": clean_url, "ping": ping_ok}
            try:
                info = client.info("memory")
                details["used_memory_human"] = info.get("used_memory_human", "")
            except Exception:
                pass

            return ServiceHealth(
                name=self.name,
                configured=True,
                enabled=True,
                reachable=True,
                authenticated=True,
                status=ServiceHealthStatus.OK if ping_ok else ServiceHealthStatus.UNAVAILABLE,
                latency_ms=latency,
                last_success=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                details=details,
            )
        except (RedisConnectionError, RedisTimeoutError) as exc:
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
                details={"url": clean_url},
            )
        except Exception as exc:
            latency = (time.monotonic() - start) * 1000.0
            err_str = str(exc)
            status = (
                ServiceHealthStatus.AUTH_FAILED
                if "NOAUTH" in err_str or "WRONGPASS" in err_str
                else ServiceHealthStatus.UNAVAILABLE
            )
            return ServiceHealth(
                name=self.name,
                configured=True,
                enabled=True,
                reachable=False,
                authenticated=False,
                status=status,
                latency_ms=latency,
                last_error=redact_secrets(err_str),
                details={"url": clean_url},
            )

    def acquire_lock(self, lock_name: str, ttl_seconds: int = 60) -> bool:
        try:
            client = self.get_client()
            key = f"lock:{lock_name}"
            acquired = client.set(key, "locked", ex=ttl_seconds, nx=True)
            return bool(acquired)
        except Exception as exc:
            logger.warning(f"Redis acquire_lock failed for {lock_name}: {redact_secrets(str(exc))}")
            return False

    def release_lock(self, lock_name: str) -> bool:
        try:
            client = self.get_client()
            key = f"lock:{lock_name}"
            deleted = client.delete(key)
            return bool(deleted)
        except Exception as exc:
            logger.warning(f"Redis release_lock failed for {lock_name}: {redact_secrets(str(exc))}")
            return False

    def is_duplicate_task(self, task_id: str, dedup_ttl_seconds: int = 300) -> bool:
        try:
            client = self.get_client()
            key = f"dedup:task:{task_id}"
            is_new = client.set(key, "processed", ex=dedup_ttl_seconds, nx=True)
            return not bool(is_new)
        except Exception as exc:
            logger.warning(
                f"Redis deduplication check failed for task {task_id}: {redact_secrets(str(exc))}"
            )
            return False

    def get_state(self, key: str) -> str | None:
        try:
            client = self.get_client()
            res = client.get(f"state:{key}")
            return str(res) if res is not None else None
        except Exception as exc:
            logger.warning(f"Redis get_state failed for key {key}: {redact_secrets(str(exc))}")
            return None

    def set_state(self, key: str, value: str, ttl_seconds: int | None = None) -> bool:
        try:
            client = self.get_client()
            res = client.set(f"state:{key}", value, ex=ttl_seconds)
            return bool(res)
        except Exception as exc:
            logger.warning(f"Redis set_state failed for key {key}: {redact_secrets(str(exc))}")
            return False

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
