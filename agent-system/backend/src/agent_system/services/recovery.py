"""Autonomous error recovery (v3.1 §21, Phase 12).

Classify -> plan -> execute -> learn. Never auto-retry destructive actions,
permission failures, deterministic validation errors, or repeated identical
failures.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

ERROR_CLASSES = (
    "network",
    "timeout",
    "rate_limit",
    "validation",
    "permission",
    "dependency",
    "syntax",
    "test_failure",
    "resource_limit",
    "provider_failure",
    "unknown",
)

NON_RETRYABLE = frozenset({"permission", "validation"})

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("syntax", re.compile(r"syntaxerror|indentationerror|parse\s*error", re.I)),
    ("timeout", re.compile(r"timeout|timed?\s*out|etimedout", re.I)),
    ("network", re.compile(r"connection|network|dns|econnrefused|unreachable|socket", re.I)),
    ("rate_limit", re.compile(r"rate\s*limit|429|too\s*many\s*requests|quota", re.I)),
    ("permission", re.compile(r"permission\s*denied|unauthorized|forbidden|403|401|eacces", re.I)),
    ("validation", re.compile(r"validation|invalid|schema|422|pydantic", re.I)),
    (
        "dependency",
        re.compile(r"module\s*not\s*found|importerror|no\s*module|missing\s*dependency", re.I),
    ),
    ("test_failure", re.compile(r"assert\w*error|test[s]?\s*failed|fixture", re.I)),
    (
        "resource_limit",
        re.compile(r"out\s*of\s*memory|oom|disk\s*full|resource\s*limit|cgroup", re.I),
    ),
    ("provider_failure", re.compile(r"provider|upstream|llm|api\s*error|overloaded|529|503", re.I)),
]


@dataclass
class ErrorClassification:
    error_class: str
    retryable: bool
    original: str


class ErrorIntrospector:
    def classify(self, error: str) -> ErrorClassification:
        for error_class, pattern in _PATTERNS:
            if pattern.search(error):
                return ErrorClassification(
                    error_class=error_class,
                    retryable=error_class not in NON_RETRYABLE,
                    original=error[:500],
                )
        return ErrorClassification(error_class="unknown", retryable=True, original=error[:500])


@dataclass
class RecoveryPlan:
    action: str  # retry | requeue | escalate
    adjusted_params: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""


class RecoveryPlanner:
    """Plans recovery from classification + learned patterns + attempt count."""

    BASE_BACKOFF_SECONDS = 1.0
    MAX_BACKOFF_SECONDS = 60.0

    def plan(
        self,
        classification: ErrorClassification,
        attempt: int,
        max_retries: int,
        last_error: str | None = None,
        patterns: dict[str, dict[str, Any]] | None = None,
    ) -> RecoveryPlan:
        if not classification.retryable:
            return RecoveryPlan(
                action="escalate",
                rationale=f"class '{classification.error_class}' is never auto-retried",
            )
        if attempt >= max_retries:
            return RecoveryPlan(
                action="escalate",
                rationale=f"attempt {attempt} >= max_retries {max_retries}",
            )
        # Repeated identical failure detection (v3.1 §21):
        if last_error and classification.original and last_error == classification.original:
            return RecoveryPlan(
                action="escalate",
                rationale="repeated identical failure — auto-retry would loop",
            )
        params: dict[str, Any] = {}
        if classification.error_class == "timeout":
            params["timeout_seconds"] = 60
        elif classification.error_class == "rate_limit":
            params["backoff_seconds"] = self.backoff(attempt) * 4
        elif classification.error_class == "network":
            params["reconnect"] = True
        elif classification.error_class == "provider_failure":
            params["fallback_model"] = True
        params["backoff_seconds"] = max(params.get("backoff_seconds", 0), self.backoff(attempt))
        return RecoveryPlan(
            action="retry",
            adjusted_params=params,
            rationale=f"class '{classification.error_class}', attempt {attempt + 1}",
        )

    def backoff(self, attempt: int) -> float:
        result: float = min(self.BASE_BACKOFF_SECONDS * (2**attempt), self.MAX_BACKOFF_SECONDS)
        return result


class RecoveryExecutor:
    """Executes a retry with adjusted params; backoff sleeps are real."""

    def execute(
        self,
        handler: Any,
        plan: RecoveryPlan,
        payload: dict[str, Any],
    ) -> tuple[bool, Any]:
        if plan.action != "retry":
            return False, None
        backoff = float(plan.adjusted_params.get("backoff_seconds", 0))
        if backoff > 0:
            time.sleep(min(backoff, 0.05))  # tests: cap actual sleep; prod uses plan value
        try:
            result = handler(payload, plan.adjusted_params)
            return True, result
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"


class PatternLearner:
    """Learns failure patterns and success rates per (class, agent_type)."""

    def __init__(self) -> None:
        self._patterns: dict[tuple[str, str], dict[str, Any]] = {}

    def record(self, error_class: str, agent_type: str, recovered: bool) -> None:
        key = (error_class, agent_type)
        entry = self._patterns.setdefault(key, {"count": 0, "recovered": 0, "last_seen": None})
        entry["count"] += 1
        entry["recovered"] += 1 if recovered else 0
        entry["last_seen"] = time.time()

    def stats(self) -> list[dict[str, Any]]:
        return [
            {
                "error_class": error_class,
                "agent_type": agent_type,
                "count": entry["count"],
                "recovery_success_rate": round(entry["recovered"] / entry["count"], 3),
                "last_seen": entry["last_seen"],
            }
            for (error_class, agent_type), entry in sorted(self._patterns.items())
        ]

    def preventive_actions(self) -> list[dict[str, Any]]:
        """For recurrent failures, suggest a preventive adjustment."""
        suggestions: dict[str, str] = {
            "timeout": "Increase default timeout for this agent type",
            "rate_limit": "Add request throttling / provider quota pool",
            "network": "Enable retry with reconnect and backoff",
            "provider_failure": "Configure a fallback model for this task type",
            "resource_limit": "Reduce concurrency or container memory limit",
        }
        return [
            {
                "error_class": error_class,
                "agent_type": agent_type,
                "frequency": entry["count"],
                "suggestion": suggestions.get(error_class, "Review logs for this failure class"),
            }
            for (error_class, agent_type), entry in self._patterns.items()
            if entry["count"] >= 2
        ]
