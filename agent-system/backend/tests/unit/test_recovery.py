"""Unit tests — error recovery (v3.1 §21)."""

from __future__ import annotations

import pytest

from agent_system.services.recovery import (
    ErrorIntrospector,
    PatternLearner,
    RecoveryExecutor,
    RecoveryPlanner,
)


class TestClassification:
    @pytest.mark.parametrize(
        ("error", "expected_class"),
        [
            ("ConnectionError: ECONNREFUSED 127.0.0.1:5432", "network"),
            ("Operation timed out after 30000ms", "timeout"),
            ("429 Too Many Requests: rate limit exceeded", "rate_limit"),
            ("PermissionError: permission denied /etc/shadow", "permission"),
            ("ValidationError: invalid schema for input", "validation"),
            ("ImportError: No module named 'pandas'", "dependency"),
            ("SyntaxError: invalid syntax at line 3", "syntax"),
            ("AssertionError: assert 2 == 3 — tests failed", "test_failure"),
            ("OOMKilled: out of memory in cgroup", "resource_limit"),
            ("ProviderError: upstream LLM overloaded (503)", "provider_failure"),
        ],
    )
    def test_known_classes(self, error: str, expected_class: str) -> None:
        result = ErrorIntrospector().classify(error)
        assert result.error_class == expected_class

    def test_unknown_class(self) -> None:
        assert ErrorIntrospector().classify("weird gibberish").error_class == "unknown"

    @pytest.mark.parametrize("error", ["permission denied", "ValidationError: bad"])
    def test_non_retryable(self, error: str) -> None:
        assert not ErrorIntrospector().classify(error).retryable


class TestPlanner:
    def test_permission_never_retried(self) -> None:
        plan = RecoveryPlanner().plan(
            ErrorIntrospector().classify("permission denied"), attempt=0, max_retries=3
        )
        assert plan.action == "escalate"

    def test_validation_never_retried(self) -> None:
        plan = RecoveryPlanner().plan(
            ErrorIntrospector().classify("ValidationError: bad input"), attempt=0, max_retries=3
        )
        assert plan.action == "escalate"

    def test_max_retries_escalates(self) -> None:
        plan = RecoveryPlanner().plan(
            ErrorIntrospector().classify("connection refused"), attempt=3, max_retries=3
        )
        assert plan.action == "escalate"

    def test_repeated_identical_failure_escalates(self) -> None:
        err = "timeout while waiting"
        plan = RecoveryPlanner().plan(
            ErrorIntrospector().classify(err), attempt=1, max_retries=3, last_error=err
        )
        assert plan.action == "escalate"

    def test_timeout_retry_increases_timeout(self) -> None:
        plan = RecoveryPlanner().plan(
            ErrorIntrospector().classify("request timed out"), attempt=0, max_retries=3
        )
        assert plan.action == "retry"
        assert plan.adjusted_params["timeout_seconds"] == 60

    def test_backoff_is_exponential_capped(self) -> None:
        planner = RecoveryPlanner()
        assert planner.backoff(0) == 1.0
        assert planner.backoff(1) == 2.0
        assert planner.backoff(3) == 8.0
        assert planner.backoff(20) == 60.0  # capped


class TestExecutor:
    def test_retry_succeeds_with_adjusted_params(self) -> None:
        """The executor re-invokes the handler ONCE with adjusted params
        (the original attempt already failed before recovery planning)."""
        received_params: list[dict] = []

        def handler(payload: dict, params: dict) -> dict:
            received_params.append(params)
            return {"ok": True, "used_timeout": params.get("timeout_seconds")}

        classification = ErrorIntrospector().classify("request timed out")
        plan = RecoveryPlanner().plan(classification, attempt=0, max_retries=3)
        assert plan.action == "retry"
        ok, result = RecoveryExecutor().execute(handler, plan, {})
        assert ok
        assert result == {"ok": True, "used_timeout": 60}
        assert received_params[0].get("timeout_seconds") == 60

    def test_retry_reports_handler_failure(self) -> None:
        def still_failing(payload: dict, params: dict) -> dict:
            raise RuntimeError("timed out again")

        plan = RecoveryPlanner().plan(
            ErrorIntrospector().classify("request timed out"), attempt=0, max_retries=3
        )
        ok, error = RecoveryExecutor().execute(still_failing, plan, {})
        assert not ok
        assert "RuntimeError" in str(error)

    def test_escalate_does_not_execute(self) -> None:
        executed = False

        def handler(payload: dict, params: dict) -> dict:
            nonlocal executed
            executed = True
            return {}

        plan = RecoveryPlanner().plan(
            ErrorIntrospector().classify("permission denied"), attempt=0, max_retries=3
        )
        ok, _ = RecoveryExecutor().execute(handler, plan, {})
        assert not ok
        assert not executed


class TestPatternLearner:
    def test_stats_and_preventive_actions(self) -> None:
        learner = PatternLearner()
        learner.record("timeout", "BrowserAgent", recovered=True)
        learner.record("timeout", "BrowserAgent", recovered=False)
        learner.record("timeout", "BrowserAgent", recovered=True)
        stats = learner.stats()
        assert stats[0]["count"] == 3
        assert stats[0]["recovery_success_rate"] == pytest.approx(0.667, abs=0.01)
        actions = learner.preventive_actions()
        assert any(a["error_class"] == "timeout" for a in actions)
        assert any("timeout" in a["suggestion"].lower() for a in actions)
