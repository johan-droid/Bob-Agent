"""Unit tests — soul loader + router identity injection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_system.infra.db import make_engine, make_session_factory
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Base
from agent_system.services.model_router import (
    ModelInfo,
    ModelRegistry,
    ModelRouter,
    PricingRegistry,
    ProviderAdapter,
    SelectionRule,
)
from agent_system.services.soul import find_soul, identity_block, load_soul


class TestLoader:
    def test_load_explicit_path(self, tmp_path: Path) -> None:
        soul = tmp_path / "SOUL.md"
        soul.write_text("# Soul\n\nYou are Bob.\n", encoding="utf-8")
        path, text = load_soul(soul)
        assert path == soul
        assert "You are Bob." in text

    def test_missing_returns_empty(self, tmp_path: Path) -> None:
        path, text = load_soul(tmp_path / "nope.md")
        assert path is None
        assert text == ""

    def test_find_in_cwd(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        assert find_soul() is None
        (tmp_path / "SOUL.md").write_text("hi", encoding="utf-8")
        assert find_soul() == tmp_path / "SOUL.md"

    def test_identity_block_format(self) -> None:
        block = identity_block("You are Bob.")
        assert block.startswith("<identity>")
        assert block.endswith("</identity>")
        assert "You are Bob." in block


class _RecordingAdapter(ProviderAdapter):
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def invoke(self, model_id: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        self.prompts.append(prompt)
        return {"output": "ok", "usage": {}}


def _router(soul_text: str | None = None) -> tuple[Any, Any, ModelRouter, _RecordingAdapter]:
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    bus = EventBus()
    pricing = PricingRegistry()
    pricing.register(
        ModelInfo(model_id="m", provider="echo", input_cost_per_1m=0, output_cost_per_1m=0)
    )
    registry = ModelRegistry()
    registry.set_rule(SelectionRule(task_type="*", primary="m"))
    router = ModelRouter(bus, pricing, registry, soul_text=soul_text)
    adapter = _RecordingAdapter()
    router.register_adapter("echo", adapter)
    return factory, bus, router, adapter


class TestRouterSoul:
    def test_soul_prepended_as_identity(self) -> None:
        factory, _, router, adapter = _router("You are Bob.")
        result = router.invoke(factory, "m", "do the thing")
        assert result.ok
        assert adapter.prompts[0].startswith("<identity>\nYou are Bob.\n</identity>")
        assert adapter.prompts[0].endswith("do the thing")

    def test_no_soul_leaves_prompt_untouched(self) -> None:
        factory, _, router, adapter = _router()
        result = router.invoke(factory, "m", "do the thing")
        assert result.ok
        assert adapter.prompts[0] == "do the thing"
