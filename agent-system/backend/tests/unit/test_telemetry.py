"""P2 — Metrics & tracing.

Acceptance: endpoint unset => zero behavior change, zero new required deps;
endpoint set => traces/metrics flow (smoke-tested when the telemetry extra
is installed, skipped otherwise); business metrics recorded at the wired
sites (router latency/cost, tool calls by risk, task durations).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_system.infra.db import make_engine, make_session_factory
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Base
from agent_system.infra.telemetry import (
    Metrics,
    current_telemetry,
    elapsed_seconds,
    get_metrics,
    setup_telemetry,
)
from agent_system.services.agent_loop import run_tool_loop
from agent_system.services.model_router import (
    EchoProvider,
    ModelInfo,
    ModelRegistry,
    ModelRouter,
    PricingRegistry,
)
from agent_system.services.tools import Tool, ToolContext, ToolRegistry


@pytest.fixture(autouse=True)
def _isolated_telemetry(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    yield setup_telemetry()
    setup_telemetry()


class TestDisabledByDefault:
    def test_unset_endpoint_is_zero_overhead_noop(self) -> None:
        handle = setup_telemetry()
        assert handle.enabled is False
        assert handle.endpoint is None
        # Only check sys.modules if telemetry extra is NOT installed
        # (when installed, opentelemetry is already loaded by other tests)
        try:
            import opentelemetry  # noqa: F401
        except ImportError:
            assert "opentelemetry" not in sys.modules

    def test_missing_sdk_warns_instead_of_raising(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Simulate a bare install (no telemetry extra): endpoint set but the
        SDK import fails ⇒ disabled with a warning, never a raise."""
        monkeypatch.setitem(sys.modules, "opentelemetry", None)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        handle = setup_telemetry()
        assert handle.enabled is False
        assert any("telemetry" in w for w in handle.warnings)


class TestInMemoryMetrics:
    def test_router_records_latency_and_cost(self, tmp_path: Path) -> None:
        engine = make_engine(f"sqlite:///{tmp_path / 'm.db'}")
        Base.metadata.create_all(engine)
        try:
            factory = make_session_factory(engine)
            pricing = PricingRegistry()
            pricing.register(
                ModelInfo(
                    model_id="m1",
                    provider="echo",
                    input_cost_per_1m=1000.0,
                    output_cost_per_1m=2000.0,
                )
            )
            router = ModelRouter(EventBus(), pricing=pricing, registry=ModelRegistry())
            router.register_adapter("echo", EchoProvider())
            router.invoke(factory, "m1", "hi", session_id="ses_x")
            snap = get_metrics().snapshot()
            assert snap["model_latency_ms"]["echo|ok"]["count"] == 1
            assert snap["cost_by_session"]["ses_x"] > 0
        finally:
            engine.dispose()

    def test_tool_calls_counted_by_risk(self) -> None:
        from types import SimpleNamespace

        def _ok(_a: Any, _c: Any) -> dict[str, Any]:
            return {"ok": True}

        registry = ToolRegistry()
        registry.register(Tool("r", "read tool", {"properties": {}}, "read", _ok))
        registry.register(Tool("w", "write tool", {"properties": {}}, "write", _ok))
        seen = {"n": 0}

        def invoke(_t: str) -> dict[str, Any]:
            seen["n"] += 1
            if seen["n"] == 1:
                return {"output": "```tool:r\n{}\n```\n```tool:w\n{}\n```"}
            return {"output": "done"}

        run_tool_loop(
            invoke=invoke,
            system="s",
            task="t",
            registry=registry,
            ctx=ToolContext(settings=SimpleNamespace()),
            max_iters=3,
        )
        counts = get_metrics().snapshot()["tool_calls"]
        assert counts.get("read|r") == 1
        assert counts.get("write|w") == 1

    def test_record_methods_never_raise(self) -> None:
        metrics = Metrics(_otel={"bogus": None})
        metrics.record_model_latency("p", 5, True)
        metrics.record_tool_call("t", "read")
        metrics.record_approval_latency("scope", 2.0)
        metrics.record_cost("s", 0.5)
        snap = metrics.snapshot()
        assert snap["model_latency_ms"]["p|ok"]["count"] == 1


class TestElapsedSeconds:
    def test_none_safe(self) -> None:
        assert elapsed_seconds(None, datetime.now(UTC)) is None

    def test_mixed_naive_aware(self) -> None:
        aware = datetime.now(UTC)
        naive = aware.replace(tzinfo=None) - timedelta(seconds=30)
        assert elapsed_seconds(naive, aware) == pytest.approx(30.0)


class TestOtelSmoke:
    def test_enabled_flow_with_collector_packages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # This smoke requires the optional `telemetry` extra. Without it the
        # production behavior under test is "stay disabled with a warning",
        # which TestDisabledByDefault already covers — skip rather than fail.
        pytest.importorskip("opentelemetry.sdk.metrics", reason="telemetry extra not installed")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        handle = setup_telemetry()
        assert handle.enabled is True
        assert current_telemetry().enabled is True
