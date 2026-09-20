"""LLM capability catalog (Bob Agentic Runtime v1 - additive layer).

c1ea179 is the frozen foundation. This module adds NO changes to the
existing task/permission/execution contracts. It provides a data-driven
capability catalog so the router can select models by requirements instead
of hard-coded "Groq = fast" style truths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ModelCapability:
    """Capability record for one provider/model pair."""

    provider: str
    model_id: str
    context_limit: int = 128_000
    input_modalities: tuple[str, ...] = ("text",)
    output_modalities: tuple[str, ...] = ("text",)
    tool_calling: bool = True
    structured_output: bool = False
    reasoning: bool = False
    coding: int = 0
    vision: bool = False
    latency_class: str = "standard"
    cost_class: str = "free"
    max_output_tokens: int = 4096

    def to_json(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "context_limit": self.context_limit,
            "input_modalities": list(self.input_modalities),
            "output_modalities": list(self.output_modalities),
            "tool_calling": self.tool_calling,
            "structured_output": self.structured_output,
            "reasoning": self.reasoning,
            "coding": self.coding,
            "vision": self.vision,
            "latency_class": self.latency_class,
            "cost_class": self.cost_class,
            "max_output_tokens": self.max_output_tokens,
        }


@dataclass
class CapabilityCatalog:
    """Mutable registry; seeded from default_catalog()."""

    _models: dict[str, ModelCapability] = field(default_factory=dict)

    def register(self, info: ModelCapability) -> None:
        self._models[f"{info.provider}/{info.model_id}"] = info

    def get(self, provider: str, model_id: str) -> ModelCapability | None:
        return self._models.get(f"{provider}/{model_id}")

    def all(self) -> list[ModelCapability]:
        return list(self._models.values())

    def filter(
        self,
        *,
        tool_calling: bool | None = None,
        min_context: int = 0,
        vision: bool | None = None,
        structured_output: bool | None = None,
        min_coding: int = 0,
        providers: list[str] | None = None,
    ) -> list[ModelCapability]:
        out: list[ModelCapability] = []
        for info in self._models.values():
            if tool_calling is not None and info.tool_calling != tool_calling:
                continue
            if info.context_limit < min_context:
                continue
            if vision is not None and info.vision != vision:
                continue
            if structured_output is not None and info.structured_output != structured_output:
                continue
            if info.coding < min_coding:
                continue
            if providers is not None and info.provider not in providers:
                continue
            out.append(info)
        return out

    def to_json(self) -> list[dict[str, Any]]:
        return [m.to_json() for m in self.all()]


def default_catalog() -> CapabilityCatalog:
    """Seed catalog covering the Agentic Runtime provider roles."""
    catalog = CapabilityCatalog()
    seeds = [
        ModelCapability(
            provider="groq",
            model_id="llama-3.3-70b-versatile",
            context_limit=131072,
            tool_calling=True,
            structured_output=True,
            coding=2,
            latency_class="fast",
            cost_class="free",
        ),
        ModelCapability(
            provider="groq",
            model_id="llama-3.1-8b-instant",
            context_limit=131072,
            tool_calling=True,
            coding=1,
            latency_class="fast",
            cost_class="free",
        ),
        ModelCapability(
            provider="groq",
            model_id="llama3-8b-8192",
            context_limit=8192,
            tool_calling=True,
            coding=1,
            latency_class="fast",
            cost_class="free",
        ),
        ModelCapability(
            provider="gemini",
            model_id="gemini-2.0-flash",
            context_limit=1048576,
            input_modalities=("text", "image", "audio", "video"),
            tool_calling=True,
            structured_output=True,
            reasoning=True,
            coding=2,
            vision=True,
            latency_class="standard",
            cost_class="free",
        ),
        ModelCapability(
            provider="gemini",
            model_id="gemini-2.0-flash-lite",
            context_limit=1048576,
            input_modalities=("text", "image"),
            tool_calling=True,
            coding=1,
            vision=True,
            latency_class="fast",
            cost_class="free",
        ),
        ModelCapability(
            provider="gemini",
            model_id="gemini-1.5-flash",
            context_limit=1048576,
            input_modalities=("text", "image"),
            tool_calling=True,
            coding=1,
            vision=True,
            latency_class="standard",
            cost_class="free",
        ),
        ModelCapability(
            provider="openrouter",
            model_id="meta-llama/llama-3.3-70b-instruct:free",
            context_limit=131072,
            tool_calling=True,
            coding=2,
            latency_class="standard",
            cost_class="free",
        ),
        ModelCapability(
            provider="openrouter",
            model_id="deepseek/deepseek-chat-v3-0324:free",
            context_limit=163840,
            tool_calling=True,
            reasoning=True,
            coding=3,
            latency_class="standard",
            cost_class="free",
        ),
        ModelCapability(
            provider="opencode",
            model_id="opencode/free-coding",
            context_limit=131072,
            tool_calling=True,
            structured_output=True,
            reasoning=True,
            coding=3,
            latency_class="standard",
            cost_class="free",
        ),
        ModelCapability(
            provider="nim",
            model_id="meta/llama-3.3-70b-instruct",
            context_limit=131072,
            tool_calling=True,
            structured_output=True,
            coding=3,
            latency_class="standard",
            cost_class="free",
            max_output_tokens=8192,
        ),
        ModelCapability(
            provider="nim",
            model_id="deepseek-ai/deepseek-r1",
            context_limit=163840,
            tool_calling=False,
            reasoning=True,
            coding=3,
            latency_class="slow",
            cost_class="free",
            max_output_tokens=8192,
        ),
        ModelCapability(
            provider="ollama_cloud",
            model_id="llama3.3",
            context_limit=131072,
            tool_calling=True,
            coding=2,
            latency_class="standard",
            cost_class="free",
        ),
        ModelCapability(
            provider="ollama_cloud",
            model_id="qwen2.5-coder",
            context_limit=131072,
            tool_calling=True,
            coding=3,
            latency_class="standard",
            cost_class="free",
        ),
        ModelCapability(
            provider="ollama",
            model_id="llama3.2",
            context_limit=131072,
            tool_calling=True,
            coding=1,
            latency_class="standard",
            cost_class="free",
        ),
        ModelCapability(
            provider="ollama",
            model_id="qwen2.5",
            context_limit=131072,
            tool_calling=True,
            coding=2,
            latency_class="standard",
            cost_class="free",
        ),
        ModelCapability(
            provider="ollama",
            model_id="phi4",
            context_limit=16384,
            tool_calling=False,
            coding=1,
            latency_class="fast",
            cost_class="free",
        ),
    ]
    for seed in seeds:
        catalog.register(seed)
    return catalog


DEFAULT_CATALOG = default_catalog()


def capability_for(
    provider: str, model_id: str, catalog: CapabilityCatalog | None = None
) -> ModelCapability:
    """Return catalog entry, or conservative unknown-model default (tool_calling=False)."""
    catalog = catalog or DEFAULT_CATALOG
    found = catalog.get(provider, model_id)
    if found is not None:
        return found
    return ModelCapability(provider=provider, model_id=model_id, tool_calling=False)


__all__ = [
    "DEFAULT_CATALOG",
    "CapabilityCatalog",
    "ModelCapability",
    "capability_for",
    "default_catalog",
]
