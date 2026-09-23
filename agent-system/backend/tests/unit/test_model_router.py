"""Unit/integration tests — model router + cost (v3.1 §19–§20)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agent_system.infra.db import make_engine, make_session_factory, session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Base, ModelCall
from agent_system.services.model_router import (
    BudgetMonitor,
    EchoProvider,
    ModelInfo,
    ModelRegistry,
    ModelRouter,
    PricingRegistry,
    ProviderAdapter,
    SelectionRule,
    UnavailableProvider,
)
from agent_system.services.skills import SkillManager


@pytest.fixture()
def env(tmp_path: Path) -> Iterator[tuple[object, object, EventBus]]:
    engine = make_engine(f"sqlite:///{tmp_path / 'router.db'}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    bus = EventBus()
    yield factory, bus, bus
    engine.dispose()


def _router_with_local_provider() -> tuple[PricingRegistry, ModelRegistry]:
    pricing = PricingRegistry()
    pricing.register(
        ModelInfo(
            model_id="local-small", provider="echo", input_cost_per_1m=0.5, output_cost_per_1m=1.5
        )
    )
    pricing.register(
        ModelInfo(
            model_id="local-big", provider="echo", input_cost_per_1m=5.0, output_cost_per_1m=15.0
        )
    )
    registry = ModelRegistry()
    registry.set_rule(
        SelectionRule(
            task_type="summary",
            primary="local-small",
            fallback="local-big",
            budget_tier="local-small",
        )
    )
    registry.set_rule(SelectionRule(task_type="reasoning", primary="local-big"))
    return pricing, registry


def test_selection_uses_configured_rules() -> None:
    _, registry = _router_with_local_provider()
    rule = registry.rule_for("summary")
    assert rule is not None and rule.primary == "local-small"
    assert registry.rule_for("nonexistent") is None


class _RecordingAdapter(ProviderAdapter):
    """Test adapter capturing the exact prompt it received."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def invoke(self, model_id: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        self.prompts.append(prompt)
        return {"output": "ok", "usage": {}}


def _skill_manager(tmp_path: Path) -> SkillManager:
    skill_dir = tmp_path / "skills" / "helper"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: helper\ndescription: Helps.\nagents: [research]\n---\n\nBe helpful.\n",
        encoding="utf-8",
    )
    return SkillManager(tmp_path / "skills")


def test_invoke_injects_matching_skills(
    env: tuple[object, object, EventBus], tmp_path: Path
) -> None:
    factory, bus, _ = env
    pricing, registry = _router_with_local_provider()
    router = ModelRouter(bus, pricing, registry, skill_manager=_skill_manager(tmp_path))
    adapter = _RecordingAdapter()
    router.register_adapter("echo", adapter)

    result = router.invoke(factory, "local-small", "do research", agent_type="research")
    assert result.ok
    assert "<skills>" in adapter.prompts[0]
    assert 'name="helper"' in adapter.prompts[0]


def test_invoke_without_manager_ignores_skills_kwarg(
    env: tuple[object, object, EventBus],
) -> None:
    factory, bus, _ = env
    pricing, registry = _router_with_local_provider()
    router = ModelRouter(bus, pricing, registry)
    router.register_adapter("echo", EchoProvider())

    result = router.invoke(factory, "local-small", "hi", skills=["helper"])
    assert result.ok


def test_invoke_records_model_call_with_exact_cost(
    env: tuple[object, object, EventBus],
) -> None:
    factory, bus, _ = env
    pricing, registry = _router_with_local_provider()
    router = ModelRouter(bus, pricing, registry)
    router.register_adapter("echo", EchoProvider())

    result = router.invoke(factory, "local-small", "hello world this is a prompt", task_id="task_1")
    assert result.ok
    assert result.cost_usd is not None and result.cost_usd > 0
    assert not result.cost_is_estimated
    assert not result.usage_is_estimated

    with session_scope(factory) as db:  # type: ignore[arg-type]
        row = db.get(ModelCall, result.model_call_id)
        assert row is not None
        assert row.model_id == "local-small"
        assert row.status == "ok"
        assert row.cost_usd == result.cost_usd


def test_unknown_provider_fails_without_crash_and_marks_estimated(
    env: tuple[object, object, EventBus],
) -> None:
    factory, bus, _ = env
    pricing, registry = _router_with_local_provider()
    router = ModelRouter(bus, pricing, registry)
    # 'local-big' has provider 'echo' but we deliberately register nothing.

    result = router.invoke(factory, "local-big", "prompt", task_id="task_2")
    assert not result.ok
    assert "no adapter" in (result.error or "")
    with session_scope(factory) as db:  # type: ignore[arg-type]
        row = db.get(ModelCall, result.model_call_id)
        assert row.status == "failed"


def test_unknown_model_pricing_marks_cost_estimated(
    env: tuple[object, object, EventBus],
) -> None:
    factory, bus, _ = env
    pricing, _ = _router_with_local_provider()
    router = ModelRouter(bus, pricing)
    router.register_adapter("echo", EchoProvider())
    # Invoke a model id with no pricing entry but a working 'echo' adapter
    # by registering pricing on the fly — instead, test via estimate=None path:
    result = router.invoke(factory, "nonexistent-model", "prompt")
    assert not result.ok  # no adapter for unknown provider
    assert result.cost_usd is None
    assert result.cost_is_estimated  # unknown cost never crashes, flagged


def test_unavailable_provider_records_failure_event(
    env: tuple[object, object, EventBus],
) -> None:
    factory, bus, _ = env
    pricing, _ = _router_with_local_provider()
    router = ModelRouter(bus, pricing)
    router.register_adapter("echo", UnavailableProvider())
    result = router.invoke(factory, "local-small", "prompt")
    assert not result.ok
    assert "provider unreachable" in (result.error or "")
    events = bus.replay_after(session_scope(factory).__enter__(), 0)  # type: ignore[attr-defined]
    assert any(e.type == "model.failed" for e in events)


def test_budget_alerts_fire_once_per_level() -> None:
    monitor = BudgetMonitor()
    monitor.set_budget("daily", 10.0)
    assert monitor.record("daily", 5.0) == [50.0]
    assert monitor.record("daily", 2.5) == [75.0]
    assert monitor.record("daily", 1.5) == [90.0]
    assert monitor.record("daily", 1.0) == [100.0]
    assert monitor.record("daily", 5.0) == []  # no repeats
    assert monitor.spent("daily") == 15.0
    assert monitor.remaining("daily") == -5.0


def test_budget_scopes_are_independent() -> None:
    monitor = BudgetMonitor()
    monitor.set_budget("task:task_1", 1.0)
    monitor.set_budget("daily", 100.0)
    assert monitor.record("task:task_1", 0.5) == [50.0]
    assert monitor.record("daily", 0.5) == []  # 0.5% — nothing fired


def test_routing_modes() -> None:
    from agent_system.services.llm_catalog import default_catalog
    from agent_system.services.llm_router import request_for_mode, route

    cat = default_catalog()
    req_fast = request_for_mode("auto/fast")
    assert req_fast.prefer_latency == "fast"

    req_coding = request_for_mode("auto/coding")
    assert req_coding.min_coding == 2
    assert req_coding.requires_tool_calling is True

    req_reasoning = request_for_mode("auto/reasoning")
    assert req_reasoning.requires_reasoning is True

    req_cheap = request_for_mode("auto/cheap")
    assert req_cheap.prefer_cost == "free"

    req_offline = request_for_mode("auto/offline")
    assert req_offline.preordered_providers == ("ollama",)

    dec = route(req_coding, cat)
    known_providers = {
        "groq",
        "nim",
        "opencode",
        "openrouter",
        "together",
        "ollama_cloud",
        "anthropic",
        "openai",
        "gemini",
        "deepseek",
        "ollama",
    }
    assert dec is not None and dec.provider in known_providers


class TestEndToEndMatrix:
    def test_classify_error_matrix(self) -> None:
        from agent_system.services.llm_router import FailureCategory, classify_error

        cat, retry = classify_error("Invalid API Key", status_code=401)
        assert cat == FailureCategory.AUTH_FAILURE and not retry

        cat, retry = classify_error("Model not found", status_code=404)
        assert cat == FailureCategory.NOT_FOUND and not retry

        cat, retry = classify_error("Rate limit exceeded", status_code=429)
        assert cat == FailureCategory.RATE_LIMIT and retry

        cat, retry = classify_error("Internal Server Error", status_code=500)
        assert cat == FailureCategory.SERVER_ERROR and retry

        cat, retry = classify_error("Connection timed out", status_code=None)
        assert cat == FailureCategory.TIMEOUT and retry

    def test_tool_calling_filtering(self) -> None:
        from agent_system.services.llm_catalog import default_catalog
        from agent_system.services.llm_router import RoutingRequest, rank_candidates

        cat = default_catalog()
        req = RoutingRequest(requires_tool_calling=True)
        ranked = rank_candidates(req, cat)
        for model_cap, _ in ranked:
            assert model_cap.tool_calling is True

    def test_free_only_filtering(self) -> None:
        from agent_system.services.llm_catalog import default_catalog
        from agent_system.services.llm_router import RoutingRequest, rank_candidates

        cat = default_catalog()
        req = RoutingRequest(prefer_cost="free")
        ranked = rank_candidates(req, cat)
        assert len(ranked) > 0
        # The top ranked candidate should be free tier
        top, _ = ranked[0]
        assert top.cost_class == "free"

    def test_fallback_chain_on_provider_error(self, env: tuple[object, object, EventBus]) -> None:
        factory, bus, _ = env
        pricing, registry = _router_with_local_provider()
        router = ModelRouter(bus, pricing, registry)

        # Primary provider fails, fallback succeeds
        from agent_system.services.fallback import invoke_with_fallback

        router.register_adapter("echo_primary", UnavailableProvider())
        router.register_adapter("echo_fallback", EchoProvider())

        pricing.register(
            ModelInfo(
                model_id="m-primary",
                provider="echo_primary",
                input_cost_per_1m=0,
                output_cost_per_1m=0,
            )
        )
        pricing.register(
            ModelInfo(
                model_id="m-fallback",
                provider="echo_fallback",
                input_cost_per_1m=0,
                output_cost_per_1m=0,
            )
        )

        candidates = [("echo_primary", "m-primary"), ("echo_fallback", "m-fallback")]
        res = invoke_with_fallback(router, factory, candidates, "test prompt")
        assert res is not None
        assert res.ok
        assert res.model_id == "m-fallback"

    def test_all_providers_unavailable_fails_cleanly(
        self, env: tuple[object, object, EventBus]
    ) -> None:
        factory, bus, _ = env
        pricing, registry = _router_with_local_provider()
        router = ModelRouter(bus, pricing, registry)

        from agent_system.services.fallback import invoke_with_fallback

        router.register_adapter("echo_f1", UnavailableProvider())
        router.register_adapter("echo_f2", UnavailableProvider())

        pricing.register(
            ModelInfo(model_id="m1", provider="echo_f1", input_cost_per_1m=0, output_cost_per_1m=0)
        )
        pricing.register(
            ModelInfo(model_id="m2", provider="echo_f2", input_cost_per_1m=0, output_cost_per_1m=0)
        )

        candidates = [("echo_f1", "m1"), ("echo_f2", "m2")]
        res = invoke_with_fallback(router, factory, candidates, "test prompt")
        assert res is not None
        assert not res.ok
        assert res.error is not None

    def test_invoke_internal_fallback_opt_in(self, env: tuple[object, object, EventBus]) -> None:
        """router.invoke(fallback=True) fails over inside one call."""
        factory, bus, _ = env
        pricing, registry = _router_with_local_provider()
        router = ModelRouter(bus, pricing, registry)

        router.register_adapter("echo_primary", UnavailableProvider())
        router.register_adapter("echo_fallback", EchoProvider())
        pricing.register(
            ModelInfo(
                model_id="m-primary",
                provider="echo_primary",
                input_cost_per_1m=0,
                output_cost_per_1m=0,
            )
        )
        pricing.register(
            ModelInfo(
                model_id="m-fallback",
                provider="echo_fallback",
                input_cost_per_1m=0,
                output_cost_per_1m=0,
            )
        )

        # Default: single attempt, failure surfaces (outer helper owns failover).
        res = router.invoke(factory, "m-primary", "hi")
        assert not res.ok

        # Opt-in: internal failover returns a working provider's output.
        res = router.invoke(factory, "m-primary", "hi", fallback=True)
        assert res.ok
        assert (res.output or "").strip()

    def test_invoke_timeout_kwarg(self, env: tuple[object, object, EventBus]) -> None:
        """Per-call timeout is accepted and adapter timeout restored."""
        factory, bus, _ = env
        pricing, registry = _router_with_local_provider()
        router = ModelRouter(bus, pricing, registry)
        adapter = EchoProvider()
        adapter.timeout = 120.0
        router.register_adapter("echo", adapter)

        res = router.invoke(factory, "local-small", "hi", timeout=5)
        assert res.ok
        assert adapter.timeout == 120.0
