"""Agentic Runtime v1 tests: catalog + health + router + durable fallback."""

from __future__ import annotations

from typing import Any

from agent_system.infra.db import make_engine, make_session_factory
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Base
from agent_system.services.fallback_ledger import should_fallback
from agent_system.services.llm_catalog import DEFAULT_CATALOG, capability_for
from agent_system.services.llm_router import (
    RoutingRequest,
    rank_candidates,
    request_for_role,
    route,
)
from agent_system.services.provider_health import (
    ProviderHealth,
    ProviderHealthTracker,
    classify_provider_error,
    is_provider_failure,
    never_fallback_reason,
)
from agent_system.services.worker_roles import WORKER_ROLES, role_for


def test_catalog_has_all_provider_roles() -> None:
    providers = {m.provider for m in DEFAULT_CATALOG.all()}
    for expected in ("groq", "gemini", "nim", "ollama", "ollama_cloud", "openrouter"):
        assert expected in providers


def test_unknown_model_is_conservative_no_tool_calling() -> None:
    cap = capability_for("groq", "no-such-model-xyz")
    assert cap.tool_calling is False


def test_tool_calling_filter_never_offers_non_tool_models() -> None:
    req = RoutingRequest(requires_tool_calling=True, min_coding=2)
    ranked = rank_candidates(req, DEFAULT_CATALOG)
    assert ranked
    assert all(c.tool_calling for c, _ in ranked)
    # deepseek-r1 (nim) has no tool calling: must never appear here.
    assert all(c.model_id != "deepseek-ai/deepseek-r1" for c, _ in ranked)


def test_role_request_maps_coder_requirements() -> None:
    req = request_for_role("CODER")
    assert req.requires_tool_calling is True
    assert req.min_coding >= 2
    assert "nim" in req.preordered_providers


def test_router_avoids_rate_limited_provider() -> None:
    health = ProviderHealthTracker()
    health.report_failure(
        "gemini", "gemini-2.0-flash", "429 RATE_LIMITED", retry_after_seconds=600.0
    )
    req = RoutingRequest(requires_tool_calling=False)
    decision = route(req, DEFAULT_CATALOG, health)
    assert decision is not None
    assert decision.provider != "gemini" or health.is_routable(decision.provider, decision.model_id)


def test_provider_failure_vs_tool_failure_semantics() -> None:
    assert is_provider_failure("429 RATE_LIMITED") is True
    assert is_provider_failure("connection timeout") is True
    assert is_provider_failure("provider 503 unavailable") is True
    # Tool failure: same LLM must retry — no provider switch.
    assert is_provider_failure("tool 'shell' failed: exit 1") is False
    assert should_fallback("429 RATE_LIMITED", 1, 3) is True
    assert should_fallback("tool failed: exit 1", 1, 3) is False
    assert should_fallback("429 RATE_LIMITED", 3, 3) is False


def test_permission_and_cancel_never_fallback() -> None:
    assert never_fallback_reason("PERMISSION_DENIED by gate") is not None
    assert never_fallback_reason("task CANCELLED by user") is not None
    assert never_fallback_reason("429 RATE_LIMITED") is None
    assert should_fallback("PERMISSION_DENIED", 1, 3) is False
    assert should_fallback("CANCELLED", 1, 3) is False


def test_health_classification() -> None:
    assert classify_provider_error("429 too many") == ProviderHealth.RATE_LIMITED
    assert classify_provider_error("401 bad key") == ProviderHealth.AUTH_FAILED
    assert classify_provider_error("500 internal") == ProviderHealth.UNAVAILABLE


def test_all_worker_roles_present() -> None:
    for role in (
        "RESEARCHER",
        "CODER",
        "DEBUGGER",
        "SECURITY_AUDITOR",
        "BROWSER_AGENT",
        "DATABASE_ENGINEER",
        "FRONTEND_ENGINEER",
        "BACKEND_ENGINEER",
        "TEST_ENGINEER",
        "DEVOPS_AGENT",
        "DOCUMENTATION_AGENT",
        "REVIEWER",
    ):
        assert role_for(role) is not None
    assert role_for("nope") is None
    assert WORKER_ROLES["CODER"].requires_tool_calling is True


class _ScriptRouter:
    """Fake ModelRouter: scripted per-model outcomes (no network)."""

    def __init__(self, outcomes: dict[str, Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    def invoke(self, factory: Any, model_id: str, prompt: str, **kwargs: Any) -> Any:
        from agent_system.services.model_router import InvocationResult

        self.calls.append(model_id)
        outcome = self.outcomes.get(model_id, {"ok": True, "output": "done"})
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, dict) and not outcome.get("ok", True):
            return InvocationResult(
                model_call_id=f"mc-{model_id}",
                model_id=model_id,
                provider="test",
                ok=False,
                tokens_in=1,
                tokens_out=1,
                tokens_cached=0,
                usage_is_estimated=False,
                cost_usd=0.0,
                cost_is_estimated=False,
                latency_ms=5,
                error=str(outcome.get("error", "boom")),
            )
        return InvocationResult(
            model_call_id=f"mc-{model_id}",
            model_id=model_id,
            provider="test",
            ok=True,
            tokens_in=1,
            tokens_out=1,
            tokens_cached=0,
            usage_is_estimated=False,
            cost_usd=0.0,
            cost_is_estimated=False,
            latency_ms=5,
            output=str(outcome.get("output", "done") if isinstance(outcome, dict) else outcome),
        )


def _factory(tmp_path: Any) -> Any:
    engine = make_engine(f"sqlite:///{tmp_path / 'fb.db'}")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def test_fallback_keeps_same_task_worker(tmp_path: Any) -> None:
    from agent_system.services.fallback import invoke_with_fallback

    factory = _factory(tmp_path)
    bus = EventBus()
    router = _ScriptRouter(
        {
            "gemini-model": {"ok": False, "error": "429 RATE_LIMITED slow down"},
            "groq-model": {"ok": True, "output": "fixed"},
        }
    )
    result = invoke_with_fallback(
        router,
        factory,
        [("gemini", "gemini-model"), ("groq", "groq-model")],
        "Fix authentication bug",
        task_id="task_1",
        worker_id="run_1",
        session_id="ses_1",
        max_attempts=3,
        bus=bus,
    )
    assert result is not None and result.ok
    assert router.calls == ["gemini-model", "groq-model"]
    assert getattr(result, "fallback_attempts", [])[0]["provider"] == "gemini"


def test_fallback_never_on_tool_failure(tmp_path: Any) -> None:
    from agent_system.services.fallback import invoke_with_fallback

    factory = _factory(tmp_path)
    router = _ScriptRouter(
        {
            "gemini-model": {"ok": False, "error": "tool 'shell' failed: exit 1"},
            "groq-model": {"ok": True, "output": "should not reach"},
        }
    )
    result = invoke_with_fallback(
        router,
        factory,
        [("gemini", "gemini-model"), ("groq", "groq-model")],
        "prompt",
        task_id="task_1",
        worker_id="run_1",
        max_attempts=3,
    )
    assert result is not None and not result.ok
    assert router.calls == ["gemini-model"]


def test_fallback_never_on_permission_denied(tmp_path: Any) -> None:
    from agent_system.services.fallback import invoke_with_fallback

    factory = _factory(tmp_path)
    router = _ScriptRouter(
        {
            "gemini-model": {"ok": False, "error": "PERMISSION_DENIED by gate"},
            "groq-model": {"ok": True, "output": "should not reach"},
        }
    )
    result = invoke_with_fallback(
        router,
        factory,
        [("gemini", "gemini-model"), ("groq", "groq-model")],
        "prompt",
        task_id="task_1",
        worker_id="run_1",
        max_attempts=3,
    )
    assert result is not None and not result.ok
    assert router.calls == ["gemini-model"]
