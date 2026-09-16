"""P1 — Circuit breaker per provider (model_router).

Acceptance: N consecutive failures trip the breaker; calls while OPEN fail
fast with ProviderUnavailableError (no network call); half-open probe + recovery
both tested; existing recovery/backoff behavior unaffected.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agent_system.infra.db import make_engine, make_session_factory, session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Base, EventRow
from agent_system.services.model_router import (
    ModelInfo,
    ModelRegistry,
    ModelRouter,
    PricingRegistry,
    ProviderAdapter,
    ProviderUnavailableError,
)


class FlakyProvider(ProviderAdapter):
    """Fails until told to recover; counts every attempted network call."""

    def __init__(self) -> None:
        self.calls = 0
        self.should_fail = True

    def invoke(self, model_id: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if self.should_fail:
            raise ConnectionError("provider down")
        return {"output": "recovered", "usage": {"input_tokens": 1, "output_tokens": 1}}


@pytest.fixture()
def env(tmp_path: Path) -> Iterator[tuple[Any, EventBus, FlakyProvider, ModelRouter]]:
    engine = make_engine(f"sqlite:///{tmp_path / 'breaker.db'}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    bus = EventBus()
    pricing = PricingRegistry()
    pricing.register(
        ModelInfo(
            model_id="flaky-1",
            provider="flaky",
            input_cost_per_1m=1.0,
            output_cost_per_1m=2.0,
        )
    )
    adapter = FlakyProvider()
    router = ModelRouter(
        bus,
        pricing=pricing,
        registry=ModelRegistry(),
        circuit_breaker_threshold=5,
        circuit_breaker_cooldown_seconds=60.0,
    )
    router.register_adapter("flaky", adapter)
    yield factory, bus, adapter, router
    engine.dispose()


def _event_types(factory: Any) -> list[str]:
    with session_scope(factory) as db:
        rows = db.query(EventRow).order_by(EventRow.sequence).all()
        return [row.type for row in rows]


def test_five_consecutive_failures_trip_breaker(env: Any) -> None:
    factory, _bus, adapter, router = env
    for _ in range(5):
        result = router.invoke(factory, "flaky-1", "hi")
        assert not result.ok
    assert adapter.calls == 5
    assert router.breaker_state("flaky") == "open"
    assert "model.circuit_opened" in _event_types(factory)


def test_open_circuit_fails_fast_without_network_call(env: Any) -> None:
    factory, _bus, adapter, router = env
    for _ in range(5):
        router.invoke(factory, "flaky-1", "hi")
    before = adapter.calls
    result = router.invoke(factory, "flaky-1", "hi")
    assert not result.ok
    assert "ProviderUnavailableError" in (result.error or "")
    assert adapter.calls == before  # no network call attempted while OPEN
    # Fast-fail is still honestly recorded as a failed ModelCall.
    assert "model.failed" in _event_types(factory)


def test_half_open_probe_recovers_and_closes(env: Any) -> None:
    factory, _bus, adapter, router = env
    for _ in range(5):
        router.invoke(factory, "flaky-1", "hi")
    assert router.breaker_state("flaky") == "open"
    # Advance past the cooldown, then recover the provider.
    breaker = router._breaker_for("flaky")
    now = {"t": 2000.0}
    breaker._clock = lambda: now["t"]
    breaker._opened_at = now["t"] - breaker.cooldown_seconds - 1.0
    adapter.should_fail = False
    result = router.invoke(factory, "flaky-1", "hi")
    assert result.ok
    assert result.output == "recovered"
    assert router.breaker_state("flaky") == "closed"
    assert "model.circuit_closed" in _event_types(factory)


def test_half_open_probe_failure_reopens_with_doubled_cooldown(env: Any) -> None:
    factory, _bus, adapter, router = env
    breaker = router._breaker_for("flaky")
    now = {"t": 1000.0}
    breaker._clock = lambda: now["t"]
    base = breaker.cooldown_seconds
    for _ in range(5):
        router.invoke(factory, "flaky-1", "hi")
    assert router.breaker_state("flaky") == "open"
    now["t"] += base + 1.0  # cooldown expires -> next call is the probe
    result = router.invoke(factory, "flaky-1", "hi")  # probe fails
    assert not result.ok
    assert router.breaker_state("flaky") == "open"
    assert breaker.cooldown_seconds == min(base * 2, 600.0)


def test_success_resets_consecutive_count(env: Any) -> None:
    factory, _bus, adapter, router = env
    for _ in range(3):
        router.invoke(factory, "flaky-1", "hi")
    adapter.should_fail = False
    assert router.invoke(factory, "flaky-1", "hi").ok
    adapter.should_fail = True
    for _ in range(4):
        router.invoke(factory, "flaky-1", "hi")
    # 3 failures, 1 success (reset), 4 failures -> still closed (needs 5).
    assert router.breaker_state("flaky") == "closed"


def test_unit_breaker_probe_serialization() -> None:
    from agent_system.services.model_router import ProviderCircuitBreaker

    now = {"t": 0.0}
    breaker = ProviderCircuitBreaker(
        "p", threshold=2, cooldown_seconds=10.0, clock=lambda: now["t"]
    )
    breaker.after_failure()
    breaker.after_failure()
    assert breaker.state == "open"
    with pytest.raises(ProviderUnavailableError):
        breaker.before_call()
    now["t"] = 11.0
    breaker.before_call()  # half-open probe allowed
    with pytest.raises(ProviderUnavailableError):
        breaker.before_call()  # second call while probe in flight: fast-fail
    assert breaker.after_success() == "closed"
