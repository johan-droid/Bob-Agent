"""Durable fallback ledger: attempts live under one worker, not new tasks."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    provider: str
    model_id: str
    ok: bool
    error: str = ""
    latency_ms: int = 0
    tool_calls_made: int = 0


def should_fallback(error: str, attempts_used: int, max_attempts: int) -> bool:
    """Provider failures may retry on another provider; tool/permission/user errors may not."""
    from agent_system.services.provider_health import is_provider_failure, never_fallback_reason

    if attempts_used >= max_attempts:
        return False
    if never_fallback_reason(error) is not None:
        return False
    return is_provider_failure(error)


def next_attempt_id(worker_id: str, attempt_no: int) -> str:
    return f"{worker_id}:attempt:{attempt_no}"


__all__ = ["AttemptRecord", "next_attempt_id", "should_fallback"]
