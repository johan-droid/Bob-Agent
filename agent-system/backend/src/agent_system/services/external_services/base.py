"""Base external service abstraction layer for Bob Agent.

Every external service driver / adapter inherits from `ExternalService` or implements
its interface:
- configuration validation
- enabled/disabled state
- connection / health check (bounded timeout, safe logging, secret redaction)
- normalized errors
- retry policy
"""

from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Redaction utilities
# ---------------------------------------------------------------------------


def redact_secrets(text: str) -> str:
    """Redact sensitive patterns (tokens, passwords, database URIs) from strings."""
    if not text:
        return ""
    result = text
    # Redact URI passwords with or without username (e.g. mongodb://user:pass@, redis://:pass@)
    result = re.sub(r"(mongodb(?:\+srv)?://[^:@]*):([^@]+)@", r"\1:***@", result)
    result = re.sub(r"(postgres(?:ql)?://[^:@]*):([^@]+)@", r"\1:***@", result)
    result = re.sub(r"(redis://[^:@]*):([^@]+)@", r"\1:***@", result)
    # Redact key-value secrets
    result = re.sub(
        r"(?i)(api[_-]?key|password|secret|token|authorization|bearer)([\s:=]+)([^\s,;&'\"]+)",
        r"\1\2***",
        result,
    )
    return result


# ---------------------------------------------------------------------------
# Health Status Data Structures
# ---------------------------------------------------------------------------


class ServiceHealthStatus(StrEnum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    AUTH_FAILED = "AUTH_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    DISABLED = "DISABLED"
    NOT_CONFIGURED = "NOT_CONFIGURED"


@dataclass
class ServiceHealth:
    name: str
    configured: bool
    enabled: bool
    reachable: bool
    authenticated: bool
    status: ServiceHealthStatus
    latency_ms: float = 0.0
    last_success: str | None = None
    last_error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "configured": self.configured,
            "enabled": self.enabled,
            "reachable": self.reachable,
            "authenticated": self.authenticated,
            "status": self.status.value,
            "latency_ms": round(self.latency_ms, 2),
            "last_success": self.last_success,
            "last_error": redact_secrets(self.last_error or "")[:300] if self.last_error else None,
            "details": {
                k: redact_secrets(str(v)) if isinstance(v, str) else v
                for k, v in self.details.items()
            },
        }


# ---------------------------------------------------------------------------
# Normalized Error Classes
# ---------------------------------------------------------------------------


class ExternalServiceError(Exception):
    """Base exception for all external service operations."""

    def __init__(self, message: str, service: str = "", code: str = "") -> None:
        clean_msg = redact_secrets(message)
        super().__init__(clean_msg)
        self.raw_message = message
        self.clean_message = clean_msg
        self.service = service
        self.code = code


class ServiceDisabledError(ExternalServiceError):
    """Raised when operating on a disabled service."""


class ServiceUnavailableError(ExternalServiceError):
    """Raised when an external service is unreachable or network times out."""


class ServiceAuthError(ExternalServiceError):
    """Raised on authentication or permission failure."""


class ServiceRateLimitError(ExternalServiceError):
    """Raised when rate-limited by an external service."""


# ---------------------------------------------------------------------------
# Retry Policy Helper
# ---------------------------------------------------------------------------


def with_retry(
    fn: Callable[[], T],
    max_retries: int = 3,
    backoff_factor: float = 0.2,
    retryable_exceptions: tuple[type[Exception], ...] = (
        ServiceUnavailableError,
        ServiceRateLimitError,
    ),
) -> T:
    """Execute a function with exponential backoff retries on retryable errors."""
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return fn()
        except retryable_exceptions as exc:
            last_exc = exc
            if attempt < max_retries:
                sleep_time = backoff_factor * (2 ** (attempt - 1))
                time.sleep(sleep_time)
            else:
                raise
        except Exception:
            raise
    if last_exc:
        raise last_exc
    raise ExternalServiceError("Retry failed without exception")


# ---------------------------------------------------------------------------
# Base Interface
# ---------------------------------------------------------------------------


class ExternalService(ABC):
    """Abstract base class for all Bob external service adapters."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Name of the external service (e.g. 'postgresql', 'mongodb', 'redis')."""

    @property
    @abstractmethod
    def is_configured(self) -> bool:
        """True if required settings/credentials are present."""

    @property
    @abstractmethod
    def is_enabled(self) -> bool:
        """True if configured AND enabled by settings."""

    @abstractmethod
    def check_health(self, timeout: float = 5.0) -> ServiceHealth:
        """Perform a bounded connection/health check returning safe ServiceHealth."""
