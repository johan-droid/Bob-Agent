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
    requests_remaining: int | None = None
    tokens_remaining: int | None = None
    latency_ms: int = 0
    success_count: int = 0
    failure_count: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "health": self.health.value,
            "consecutive_failures": self.consecutive_failures,
            "retry_after": self.retry_after,
            "requests_remaining": self.requests_remaining,
            "tokens_remaining": self.tokens_remaining,
            "latency_ms": self.latency_ms,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "last_error": self.last_error[:300],
        }


def classify_provider_error(error: str) -> ProviderHealth:
    """Classify a raw adapter error into a health state (additive helper).

    Delegates to the structured llm_contract taxonomy when available so
    TOOL_UNSUPPORTED / STRUCTURED_OUTPUT_UNSUPPORTED / QUOTA_EXHAUSTED map
    to non-routable states instead of generic DEGRADED.
    """
    try:
        from agent_system.services.llm_contract import ProviderErrorCode as _Code
        from agent_system.services.llm_contract import classify_provider_error as _classify

        err = _classify(error)
        mapping = {
            _Code.AUTHENTICATION: ProviderHealth.AUTH_FAILED,
            _Code.INVALID_REQUEST: ProviderHealth.DISABLED,
            _Code.INVALID_MODEL: ProviderHealth.DISABLED,
            _Code.MODEL_UNAVAILABLE: ProviderHealth.DISABLED,
            _Code.RATE_LIMITED: ProviderHealth.RATE_LIMITED,
            _Code.QUOTA_EXHAUSTED: ProviderHealth.RATE_LIMITED,
            _Code.TIMEOUT: ProviderHealth.UNAVAILABLE,
            _Code.NETWORK: ProviderHealth.UNAVAILABLE,
            _Code.SERVER_ERROR: ProviderHealth.UNAVAILABLE,
            _Code.TOOL_UNSUPPORTED: ProviderHealth.DISABLED,
            _Code.STRUCTURED_OUTPUT_UNSUPPORTED: ProviderHealth.DISABLED,
            _Code.CONTENT_POLICY: ProviderHealth.DISABLED,
            _Code.UNKNOWN: ProviderHealth.DEGRADED,
        }
        return mapping.get(err.code, ProviderHealth.DEGRADED)
    except ImportError:
        pass
    text = (error or "").upper()
    if "TOOL_UNSUPPORTED" in text or "STRUCTURED_OUTPUT_UNSUPPORTED" in text:
        return ProviderHealth.DISABLED
    if "QUOTA" in text or "EXHAUSTED" in text or "BILLING" in text:
        return ProviderHealth.RATE_LIMITED
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

    def update_rate_limits(self, provider: str, model_id: str, headers: dict[str, Any]) -> None:
        """Parse provider rate limit / reset headers (Groq, OpenAI, etc.).

        Header lookup is case-insensitive; honours ``Retry-After`` when
        supplied and applies conservative adaptive cooldowns otherwise.
        """
        if not headers:
            return
        lowered = {str(k).lower(): v for k, v in headers.items()}
        with self._lock:
            key = self._key(provider, model_id)
            st = self._status.get(key)
            if st is None:
                st = ProviderStatus(provider=provider, model_id=model_id)
                self._status[key] = st

            rem_req = lowered.get("x-ratelimit-remaining-requests")
            if rem_req is not None:
                try:
                    st.requests_remaining = int(rem_req)  # type: ignore[arg-type]
                except (ValueError, TypeError):
                    pass

            rem_tok = lowered.get("x-ratelimit-remaining-tokens")
            if rem_tok is not None:
                try:
                    st.tokens_remaining = int(rem_tok)  # type: ignore[arg-type]
                except (ValueError, TypeError):
                    pass

            retry_after = lowered.get("retry-after")
            if retry_after is not None:
                try:
                    secs = float(retry_after)  # type: ignore[arg-type]
                    st.retry_after = time.monotonic() + max(1.0, secs)
                    st.health = ProviderHealth.RATE_LIMITED
                except (ValueError, TypeError):
                    pass

            if (st.requests_remaining is not None and st.requests_remaining <= 0) or (
                st.tokens_remaining is not None and st.tokens_remaining <= 0
            ):
                st.health = ProviderHealth.RATE_LIMITED
                if st.retry_after <= time.monotonic():
                    st.retry_after = time.monotonic() + 60.0

    def report_success(self, provider: str, model_id: str = "", latency_ms: int = 0) -> None:
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
            st.latency_ms = latency_ms
            st.success_count += 1
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
            st.failure_count += 1
            try:
                from agent_system.services.secrets import redact_value as _redact

                st.last_error = _redact(error)[:300]
            except Exception:
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

    def cooldown_remaining(self, provider: str, model_id: str = "") -> float:
        """Seconds until a rate-limited model is routable again (0 when routable)."""
        st = self.get(provider, model_id)
        if st.health != ProviderHealth.RATE_LIMITED:
            return 0.0
        return max(0.0, st.retry_after - time.monotonic())

    @property
    def cooldown_until(self) -> dict[str, float]:
        """Snapshot of active cooldowns keyed by provider/model (observability)."""
        with self._lock:
            return {
                key: st.retry_after
                for key, st in self._status.items()
                if st.health == ProviderHealth.RATE_LIMITED
            }

    def is_routable(self, provider: str, model_id: str = "") -> bool:
        st = self.get(provider, model_id)
        # Authentication failures never hammer the same key; invalid models
        # never retry the same model.
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


GLOBAL_HEALTH_TRACKER = ProviderHealthTracker()

__all__ = [
    "GLOBAL_HEALTH_TRACKER",
    "ProviderHealth",
    "ProviderHealthTracker",
    "ProviderStatus",
    "classify_provider_error",
    "is_provider_failure",
    "never_fallback_reason",
]
