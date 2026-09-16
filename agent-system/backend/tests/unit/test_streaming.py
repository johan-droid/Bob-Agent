"""P2 — Token-level streaming.

Acceptance: a streaming provider emits incremental model.token events and
closes with model.completed; cost/usage accounting is identical to the
non-streaming path; non-streaming adapters fall back gracefully.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from agent_system.infra.db import make_engine, make_session_factory, session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Base, EventRow, ModelCall
from agent_system.services.model_router import (
    EchoProvider,
    ModelInfo,
    ModelRegistry,
    ModelRouter,
    PricingRegistry,
    ProviderAdapter,
)


class NonStreamingAdapter(ProviderAdapter):
    """Legacy adapter without stream support (fallback path)."""

    supports_streaming = False

    def invoke(self, model_id: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "output": "full answer",
            "usage": {"input_tokens": 10, "output_tokens": 5, "cached_tokens": 0},
        }


def _env(tmp_path: Path) -> Any:
    engine = make_engine(f"sqlite:///{tmp_path / 'stream.db'}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    bus = EventBus()
    pricing = PricingRegistry()
    pricing.register(
        ModelInfo(model_id="echo-1", provider="echo", input_cost_per_1m=1.0, output_cost_per_1m=2.0)
    )
    pricing.register(
        ModelInfo(
            model_id="legacy-1",
            provider="legacy",
            input_cost_per_1m=1.0,
            output_cost_per_1m=2.0,
        )
    )
    router = ModelRouter(bus, pricing=pricing, registry=ModelRegistry())
    router.register_adapter("echo", EchoProvider())
    router.register_adapter("legacy", NonStreamingAdapter())
    return factory, bus, router, engine


def _events(factory: Any, etype: str) -> list[Any]:
    with session_scope(factory) as db:
        rows = db.query(EventRow).filter_by(type=etype).order_by(EventRow.sequence).all()
        return [(r.type, dict(r.payload or {})) for r in rows]


class TestStreamingEcho:
    def test_incremental_tokens_then_completed(self, tmp_path: Path) -> None:
        factory, _bus, router, engine = _env(tmp_path)
        try:
            deltas: list[str] = []
            result = router.invoke_streaming(
                factory, "echo-1", "hello world", on_token=deltas.append
            )
            assert result.ok
            tokens = _events(factory, "model.token")
            assert len(tokens) > 1  # genuinely incremental
            assert "".join(deltas) == result.output
            assert "".join(t["delta"] for _, t in tokens) == result.output
            completed = _events(factory, "model.completed")
            assert len(completed) == 1
        finally:
            engine.dispose()

    def test_accounting_identical_to_non_streaming(self, tmp_path: Path) -> None:
        factory, _bus, router, engine = _env(tmp_path)
        try:
            streamed = router.invoke_streaming(factory, "echo-1", "same prompt")
            plain = router.invoke(factory, "echo-1", "same prompt")
            assert streamed.output == plain.output
            assert streamed.tokens_in == plain.tokens_in
            assert streamed.tokens_out == plain.tokens_out
            assert streamed.cost_usd == plain.cost_usd
            assert streamed.cost_is_estimated == plain.cost_is_estimated
            assert streamed.usage_is_estimated == plain.usage_is_estimated
        finally:
            engine.dispose()

    def test_model_call_row_recorded_once(self, tmp_path: Path) -> None:
        factory, _bus, router, engine = _env(tmp_path)
        try:
            router.invoke_streaming(factory, "echo-1", "hi")
            with session_scope(factory) as db:
                rows = db.query(ModelCall).filter_by(model_id="echo-1").all()
            assert len(rows) == 1
            assert rows[0].status == "ok"
        finally:
            engine.dispose()


class TestFallback:
    def test_non_streaming_adapter_single_token_shape(self, tmp_path: Path) -> None:
        factory, _bus, router, engine = _env(tmp_path)
        try:
            result = router.invoke_streaming(factory, "legacy-1", "hi")
            assert result.ok
            assert result.output == "full answer"
            tokens = _events(factory, "model.token")
            assert len(tokens) == 1  # same event shape, one chunk
            assert tokens[0][1]["delta"] == "full answer"
            assert len(_events(factory, "model.completed")) == 1
        finally:
            engine.dispose()

    def test_streaming_failure_records_failed_call(self, tmp_path: Path) -> None:
        class BoomAdapter(ProviderAdapter):
            supports_streaming = True

            def invoke(self, *a: Any, **k: Any) -> dict[str, Any]:
                raise AssertionError("must not be called")

            def stream(self, *a: Any, **k: Any) -> Any:
                def _gen() -> Iterator[str]:
                    yield "partial"
                    raise ConnectionError("mid-stream cut")

                return _gen(), {}

        factory, _bus, router, engine = _env(tmp_path)
        pricing = router.pricing
        pricing.register(
            ModelInfo(
                model_id="boom-1",
                provider="boom",
                input_cost_per_1m=1.0,
                output_cost_per_1m=1.0,
            )
        )
        router.register_adapter("boom", BoomAdapter())
        try:
            result = router.invoke_streaming(factory, "boom-1", "hi")
            assert not result.ok
            assert "ConnectionError" in (result.error or "")
            assert len(_events(factory, "model.failed")) == 1
        finally:
            engine.dispose()
