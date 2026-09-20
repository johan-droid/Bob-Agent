"""Provider health (Agentic Runtime v1, additive)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ProviderHealth(StrEnum):
    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_FAILED = "AUTH_FAILED"
    UNAVAILABLE = "UNAVAILABLE"
    DISABLED = "DISABLED"


@dataclass
class ProviderStatus:
    provider: str
    model_id: str = ""
    health: ProviderHealth = ProviderHealth.AVAILABLE
    consecutive_failures: int = 0
    retry_after: float = 0.0
    last_error: str = ""
    updated_at: float = field(default_factory=time.monotonic)

    def to_json(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "health": self.health.value,
            "consecutive_failures": self.consecutive_failures,
            "retry_after": self.retry_after,
            "last_error": self.last_error[:300],
        }


def classify_provider_error(error: str) -> ProviderHealth:
    """Classify a raw adapter error into a health state (additive helper)."""
    text = (error or "").upper()
    if "404" in text or "NOT FOUND" in text or "DOES NOT EXIST" in text or "UNKNOWN MODEL" in text:
        return ProviderHealth.DISABLED
    if "400" in text or "INVALID_REQUEST" in text or "BAD REQUEST" in text:
        return ProviderHealth.DISABLED
    if "401" in text or "403" in text or "AUTH" in text or "API_KEY" in text:
        return ProviderHealth.AUTH_FAILED
    if "429" in text or "RATE_LIMIT" in text or "RETRY_AFTER" in text:
        return ProviderHealth.RATE_LIMITED
    if "TIMEOUT" in text or "CONNECTION" in text or "5" in text[:3]:
        return ProviderHealth.UNAVAILABLE
    if "500" in text or "502" in text or "503" in text or "504" in text:
        return ProviderHealth.UNAVAILABLE
    if "UNREACHABLE" in text or "NO ADAPTER" in text:
        return ProviderHealth.UNAVAILABLE
    return ProviderHealth.DEGRADED


def is_provider_failure(error: str) -> bool:
    """True when the error means the *provider* failed (fallback allowed)."""
    if not error:
        return False
    text = error.upper()
    markers = (
        "404",
        "400",
        "401",
        "403",
        "NOT FOUND",
        "BAD REQUEST",
        "429",
        "RATE_LIMIT",
        "TIMEOUT",
        "CONNECTION",
        "UNREACHABLE",
        "NO ADAPTER",
        "500",
        "502",
        "503",
        "504",
        "TEMPORARY",
        "UNAVAILABLE",
        "NETWORK",
        "DNS",
    )
    return any(m in text for m in markers)


def never_fallback_reason(error: str) -> str | None:
    """Return STOP reason when fallback is forbidden, else None.

    Permission denials and user cancellation must never trigger a provider
    switch — they describe the *request*, not the provider.
    """
    text = (error or "").upper()
    if "PERMISSION_DENIED" in text or "APPROVAL_DENIED" in text:
        return "permission_denied_stop"
    if "CANCELLED" in text or "CANCELED" in text:
        return "cancelled_stop"
    return None


class ProviderHealthTracker:
    """Thread-safe in-memory health per provider/model (additive)."""

    def __init__(self) -> None:
        import threading

        self._lock = threading.Lock()
        self._status: dict[str, ProviderStatus] = {}

    def _key(self, provider: str, model_id: str) -> str:
        return f"{provider}/{model_id or ''}"

    def get(self, provider: str, model_id: str = "") -> ProviderStatus:
        with self._lock:
            key = self._key(provider, model_id)
            existing = self._status.get(key)
            if existing is None:
                existing = ProviderStatus(provider=provider, model_id=model_id)
                self._status[key] = existing
            if existing.health == ProviderHealth.RATE_LIMITED:
                if time.monotonic() >= existing.retry_after:
                    existing.health = ProviderHealth.AVAILABLE
                    existing.retry_after = 0.0
            return existing

    def report_success(self, provider: str, model_id: str = "") -> None:
        with self._lock:
            key = self._key(provider, model_id)
            st = self._status.get(key)
            if st is None:
                st = ProviderStatus(provider=provider, model_id=model_id)
                self._status[key] = st
            st.health = ProviderHealth.AVAILABLE
            st.consecutive_failures = 0
            st.retry_after = 0.0
            st.last_error = ""
            st.updated_at = time.monotonic()

    def report_failure(
        self,
        provider: str,
        model_id: str = "",
        error: str = "",
        retry_after_seconds: float = 60.0,
    ) -> ProviderStatus:
        with self._lock:
            key = self._key(provider, model_id)
            st = self._status.get(key)
            if st is None:
                st = ProviderStatus(provider=provider, model_id=model_id)
                self._status[key] = st
            st.health = classify_provider_error(error)
            st.consecutive_failures += 1
            st.last_error = error[:300]
            st.updated_at = time.monotonic()
            if st.health == ProviderHealth.RATE_LIMITED:
                st.retry_after = time.monotonic() + max(1.0, retry_after_seconds)
            return st

    def mark_disabled(self, provider: str, model_id: str = "") -> None:
        with self._lock:
            key = self._key(provider, model_id)
            st = self._status.get(key)
            if st is None:
                st = ProviderStatus(provider=provider, model_id=model_id)
                self._status[key] = st
            st.health = ProviderHealth.DISABLED
            st.updated_at = time.monotonic()

    def is_routable(self, provider: str, model_id: str = "") -> bool:
        st = self.get(provider, model_id)
        if st.health in (ProviderHealth.DISABLED, ProviderHealth.AUTH_FAILED):
            return False
        if st.health == ProviderHealth.RATE_LIMITED:
            return time.monotonic() >= st.retry_after
        if st.health == ProviderHealth.UNAVAILABLE and st.consecutive_failures >= 5:
            return False
        return True

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [s.to_json() for s in self._status.values()]


__all__ = [
    "ProviderHealth",
    "ProviderHealthTracker",
    "ProviderStatus",
    "classify_provider_error",
    "is_provider_failure",
    "never_fallback_reason",
]
