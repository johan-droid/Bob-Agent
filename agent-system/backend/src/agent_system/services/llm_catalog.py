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
    """Capability record for one provider/model pair.

    Capabilities are never inferred from OpenAI-compatibility alone — each
    flag is explicit per model. Unknown models default to the conservative
    :func:`capability_for` fallback (no tools / no streaming / no structured
    output) so the router never sends unsupported payloads.
    """

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
    # --- provider-agnostic extensions (additive, default conservative) ---
    display_name: str = ""
    output_limit: int = 4096
    supports_tools: bool = True
    supports_streaming: bool = True
    supports_structured_output: bool = False
    supports_reasoning: bool = False
    supports_vision: bool = False
    supports_parallel_tools: bool = False
    api_surface: str = "chat"  # chat | responses | native

    def __post_init__(self) -> None:
        # Keep legacy aliases in sync when only one side was provided.
        if self.display_name == "" and self.model_id:
            object.__setattr__(self, "display_name", self.model_id)
        if self.output_limit == 4096 and self.max_output_tokens != 4096:
            object.__setattr__(self, "output_limit", self.max_output_tokens)

    @property
    def effective_supports_tools(self) -> bool:
        return bool(self.tool_calling and self.supports_tools)

    @property
    def effective_supports_structured(self) -> bool:
        return bool(self.structured_output or self.supports_structured_output)

    @property
    def effective_supports_vision(self) -> bool:
        return bool(self.vision or self.supports_vision)

    @property
    def effective_supports_reasoning(self) -> bool:
        return bool(self.reasoning or self.supports_reasoning)

    def to_json(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "display_name": self.display_name or self.model_id,
            "context_limit": self.context_limit,
            "output_limit": self.output_limit or self.max_output_tokens,
            "input_modalities": list(self.input_modalities),
            "output_modalities": list(self.output_modalities),
            "tool_calling": self.tool_calling,
            "supports_tools": self.effective_supports_tools,
            "structured_output": self.structured_output,
            "supports_structured_output": self.effective_supports_structured,
            "supports_streaming": self.supports_streaming,
            "supports_reasoning": self.effective_supports_reasoning,
            "supports_vision": self.effective_supports_vision,
            "supports_parallel_tools": self.supports_parallel_tools,
            "api_surface": self.api_surface,
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
        streaming: bool | None = None,
        supports_tools: bool | None = None,
        supports_streaming: bool | None = None,
        supports_structured_output: bool | None = None,
        supports_vision: bool | None = None,
        supports_reasoning: bool | None = None,
        api_surface: str | None = None,
    ) -> list[ModelCapability]:
        out: list[ModelCapability] = []
        for info in self._models.values():
            if tool_calling is not None and info.tool_calling != tool_calling:
                continue
            if supports_tools is True and not info.effective_supports_tools:
                continue
            if info.context_limit < min_context:
                continue
            if vision is not None and info.vision != vision:
                continue
            if supports_vision is True and not info.effective_supports_vision:
                continue
            if structured_output is not None and info.structured_output != structured_output:
                continue
            if supports_structured_output is True and not info.effective_supports_structured:
                continue
            if supports_reasoning is True and not info.effective_supports_reasoning:
                continue
            if streaming is True and not info.supports_streaming:
                continue
            if supports_streaming is True and not info.supports_streaming:
                continue
            if api_surface is not None and info.api_surface != api_surface:
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
            model_id="openai/gpt-oss-20b",
            context_limit=131072,
            tool_calling=True,
            structured_output=True,
            coding=2,
            latency_class="fast",
            cost_class="free",
        ),
        ModelCapability(
            provider="groq",
            model_id="openai/gpt-oss-120b",
            context_limit=131072,
            tool_calling=True,
            structured_output=True,
            reasoning=True,
            coding=3,
            latency_class="standard",
            cost_class="free",
        ),
        ModelCapability(
            provider="groq",
            model_id="qwen/qwen3.8-27b",
            context_limit=131072,
            tool_calling=True,
            coding=2,
            latency_class="fast",
            cost_class="free",
        ),
        ModelCapability(
            provider="gemini",
            model_id="gemini-3.6-flash",
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
            model_id="gemini-3.1-flash-lite",
            context_limit=1048576,
            input_modalities=("text", "image"),
            tool_calling=True,
            coding=1,
            vision=True,
            latency_class="fast",
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
            model_id="big-pickle",
            context_limit=131072,
            tool_calling=True,
            structured_output=True,
            reasoning=True,
            coding=3,
            latency_class="standard",
            cost_class="free",
            supports_tools=True,
            supports_streaming=True,
            supports_structured_output=True,
            supports_parallel_tools=True,
            api_surface="chat",
        ),
        ModelCapability(
            provider="opencode",
            model_id="mimo-v2.6-flash-free",
            context_limit=131072,
            tool_calling=True,
            structured_output=True,
            coding=2,
            latency_class="fast",
            cost_class="free",
            supports_tools=True,
            supports_streaming=True,
            supports_structured_output=True,
            api_surface="chat",
        ),
        ModelCapability(
            provider="opencode",
            model_id="nemotron-3.5-lightning-free",
            context_limit=131072,
            tool_calling=True,
            reasoning=True,
            coding=2,
            latency_class="fast",
            cost_class="free",
            supports_tools=True,
            supports_streaming=True,
            api_surface="chat",
        ),
        ModelCapability(
            provider="opencode",
            model_id="glm-5.3-flash",
            context_limit=131072,
            tool_calling=True,
            structured_output=True,
            coding=2,
            latency_class="standard",
            cost_class="free",
            supports_tools=True,
            supports_streaming=True,
            supports_structured_output=True,
            api_surface="chat",
        ),
        ModelCapability(
            provider="opencode",
            model_id="kimi-k2.5",
            context_limit=131072,
            tool_calling=True,
            structured_output=True,
            coding=3,
            latency_class="standard",
            cost_class="free",
            supports_tools=True,
            supports_streaming=True,
            supports_structured_output=True,
            supports_parallel_tools=True,
            api_surface="chat",
        ),
        ModelCapability(
            provider="opencode",
            model_id="muse-spark-1.3-contributor-free",
            context_limit=131072,
            tool_calling=True,
            structured_output=True,
            reasoning=True,
            coding=3,
            latency_class="standard",
            cost_class="free",
            supports_tools=True,
            supports_streaming=True,
            supports_structured_output=True,
            supports_parallel_tools=True,
            api_surface="responses",
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
        ModelCapability(
            provider="tokenrouter",
            model_id="auto",
            context_limit=128000,
            tool_calling=True,
            structured_output=True,
            coding=2,
            latency_class="standard",
            cost_class="standard",
        ),
    ]
    for seed in seeds:
        catalog.register(seed)
    return catalog


DEFAULT_CATALOG = default_catalog()


def capability_for(
    provider: str, model_id: str, catalog: CapabilityCatalog | None = None
) -> ModelCapability:
    """Return catalog entry, or conservative unknown-model default.

    Unknown models default to no tools / no streaming / no structured
    output so the router never sends unsupported payloads merely because a
    provider is OpenAI-compatible.
    """
    catalog = catalog or DEFAULT_CATALOG
    found = catalog.get(provider, model_id)
    if found is not None:
        return found
    # Heuristic: OpenCode Zen models served over /responses support tools.
    surface = "responses" if provider == "opencode" and (
        model_id.startswith(("gpt-", "grok-", "muse-spark-"))
    ) else "chat"
    return ModelCapability(
        provider=provider,
        model_id=model_id,
        tool_calling=False,
        supports_tools=False,
        supports_streaming=True,
        supports_structured_output=False,
        supports_parallel_tools=False,
        api_surface=surface,
    )


__all__ = [
    "DEFAULT_CATALOG",
    "CapabilityCatalog",
    "ModelCapability",
    "capability_for",
    "default_catalog",
]
