"""Real LLM provider adapters (optional; system boots without any).

Implements the `ProviderAdapter` interface from `model_router.py` for the
providers the setup wizard offers. All adapters are pure HTTP clients (httpx,
already a dependency) — no heavy SDKs.

Supported transports:

- **OpenAI-compatible** (`/v1/chat/completions`): Groq, Ollama, OpenRouter,
  Together, Mistral, DeepSeek, HuggingFace router, TokenRouter, NIM,
  Ollama Cloud, OpenCode Zen, and OpenAI itself.
- **Gemini native** (`:generateContent`, `x-goog-api-key` header).
- **Anthropic native** (`/v1/messages`, `x-api-key` + `anthropic-version`).

Every adapter sends documented auth headers plus an optional user-supplied
`extra_headers` blob (from `PROVIDER_EXTRA_HEADERS`). Credentials never get
logged. Model pricing defaults are per-provider; unknown pricing is flagged
`estimated` by the router, never fabricated.

**Tool-call carrier (one shape, every provider):** each adapter normalizes the
provider-native structured calls (OpenAI ``message.tool_calls``, Gemini
``functionCall`` parts, Anthropic ``tool_use`` blocks — and the fragments
streamed over SSE) into a canonical ``"tool_calls"`` list on the adapter result
dict: ``[{"id", "name", "arguments"}]`` where ``arguments`` is a JSON string.
Both ``invoke`` and ``stream`` produce this shape, so downstream execution
(""router -> agent loop -> protocol parser"") never branches per provider.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from agent_system.config import Settings

# ---------------------------------------------------------------------------
# Catalog metadata (base URLs, default models, auth)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderSpec:
    key: str
    label: str
    base_url: str
    default_model: str
    auth: str = "bearer"  # bearer | header | none | custom
    default_headers: dict[str, str] = field(default_factory=dict)
    description: str = ""
    free_tier: bool = False
    models: tuple[str, ...] = ()
    #: Default wire surface for this provider ("chat" = OpenAI
    #: ``/chat/completions``, "responses" = OpenAI ``/responses``).
    api_surface: str = "chat"
    #: Per-model surface overrides as ``(model_id_prefix, surface)`` pairs —
    # needed for gateways that mix surfaces under one base URL (OpenCode Zen
    # serves GPT/Grok/Muse over ``/responses`` but GLM/Kimi/DeepSeek over
    # ``/chat/completions``). First matching prefix wins; default otherwise.
    model_surfaces: tuple[tuple[str, str], ...] = ()


PROVIDERS: dict[str, ProviderSpec] = {
    "openai": ProviderSpec(
        key="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        auth="bearer",
        description="Official OpenAI API.",
        models=("gpt-4o-mini", "gpt-4o", "gpt-5-mini"),
    ),
    "anthropic": ProviderSpec(
        key="anthropic",
        label="Anthropic",
        base_url="https://api.anthropic.com/v1",
        default_model="claude-sonnet-4-5",
        auth="custom",
        description="Official Anthropic Claude API (Messages format).",
        models=("claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-4-5"),
    ),
    "groq": ProviderSpec(
        key="groq",
        label="Groq",
        base_url="https://api.groq.com/openai/v1",
        default_model="openai/gpt-oss-20b",
        auth="bearer",
        description="Ultra-fast inference; free tier available.",
        free_tier=True,
        models=(
            "openai/gpt-oss-20b",
            "openai/gpt-oss-120b",
            "openai/gpt-oss-safeguard-20b",
            "qwen/qwen3.8-27b",
            "allam-2-7b",
        ),
    ),
    "ollama": ProviderSpec(
        key="ollama",
        label="Ollama (local)",
        base_url="http://localhost:11434/v1",
        default_model="llama3.2",
        auth="none",
        description="Local models via Ollama. No API key needed.",
        free_tier=True,
        models=("llama3.2", "qwen2.5", "phi4"),
    ),
    "openrouter": ProviderSpec(
        key="openrouter",
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        default_model="qwen/qwen3.8-27b:free",
        auth="bearer",
        description="Aggregator with many free models.",
        free_tier=True,
        models=(
            "qwen/qwen3.8-27b:free",
            "google/gemma-4-31b-it:free",
            "nvidia/nemotron-3.5-lightning:free",
            "z-ai/glm-5.2:free",
        ),
    ),
    "together": ProviderSpec(
        key="together",
        label="Together AI",
        base_url="https://api.together.xyz/v1",
        default_model="meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
        auth="bearer",
        description="Open-source model hosting; free tier models.",
        free_tier=True,
        models=(
            "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
            "Qwen/Qwen2.5-72B-Instruct-Turbo",
            "google/gemma-2-27b-it",
        ),
    ),
    "mistral": ProviderSpec(
        key="mistral",
        label="Mistral AI",
        base_url="https://api.mistral.ai/v1",
        default_model="open-mistral-7b",
        auth="bearer",
        description="European model lab; free tier available.",
        free_tier=True,
        models=("open-mistral-7b", "mistral-small-latest", "mistral-medium-latest"),
    ),
    "gemini": ProviderSpec(
        key="gemini",
        label="Google Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        default_model="gemini-3.6-flash",
        auth="header",
        default_headers={"x-goog-api-key": "{api_key}"},
        description="Google AI Studio free tier.",
        free_tier=True,
        models=("gemini-3.6-flash", "gemini-3.1-flash-lite"),
    ),
    "nim": ProviderSpec(
        key="nim",
        label="NVIDIA NIM",
        base_url="https://integrate.api.nvidia.com/v1",
        default_model="meta/llama-3.3-70b-instruct",
        auth="bearer",
        description="Coding/reasoning models via NVIDIA NIM (OpenAI-compatible).",
        models=("meta/llama-3.3-70b-instruct", "deepseek-ai/deepseek-r1"),
    ),
    "ollama_cloud": ProviderSpec(
        key="ollama_cloud",
        label="Ollama Cloud",
        base_url="https://ollama.com/v1",
        default_model="gpt-oss:20b",
        auth="bearer",
        description="Hosted open models via Ollama Cloud (OpenAI-compatible).",
        # Free tier: Ollama Cloud is Bob's PRIMARY inference platform, so it
        # must survive the zero-cost filter (BOB_ZERO_COST_MODE) that gates
        # every other routing decision.
        free_tier=True,
        # Free-plan verified set (2026-09-24): every id answers 200 on the
        # free tier. Models outside the plan (e.g. qwen3.5:397b,
        # deepseek-v4.1-flash) answer HTTP 402 "not included in your free
        # usage" and are deliberately not offered.
        models=(
            "gpt-oss:20b",
            "gpt-oss:120b",
            "gemma4:31b",
            "nemotron-3-nano:30b",
            "nemotron-3-super",
            "nemotron-3-ultra",
        ),
    ),
    "opencode": ProviderSpec(
        key="opencode",
        label="OpenCode Zen",
        base_url="https://opencode.ai/zen/v1",
        default_model="big-pickle",
        auth="bearer",
        description=(
            "OpenCode Zen gateway. Mixed API surfaces per model: GLM/Kimi/"
            "DeepSeek/MiMo/Nemotron over /chat/completions, GPT/Grok/Muse over "
            "/responses. Free-tier models are only served to the OpenCode "
            "client itself; funded keys work here."
        ),
        free_tier=True,
        # Only models whose surface Bob actually speaks. Excluded on purpose:
        # jev-* (custom /systemone evaluator protocol, not a chat API) and
        # gemini-* through Zen (proxied native generateContent — use the
        # dedicated Google Gemini provider instead).
        models=(
            "big-pickle",
            "mimo-v2.6-flash-free",
            "mimo-v2.5-free",
            "ling-3.0-flash-fin-free",
            "nemotron-3-ultra-free",
            "nemotron-3.5-lightning-free",
            "glm-5.3-flash",
            "kimi-k2.5",
            "muse-spark-1.3-contributor-free",
        ),
        model_surfaces=(
            ("gpt-", "responses"),
            ("grok-", "responses"),
            ("muse-spark-", "responses"),
        ),
    ),
    "deepseek": ProviderSpec(
        key="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        default_model="deepseek-chat",
        auth="bearer",
        description="Very cheap reasoning models (OpenAI-compatible).",
        models=("deepseek-chat", "deepseek-reasoner"),
    ),
    "huggingface": ProviderSpec(
        key="huggingface",
        label="HuggingFace",
        base_url="https://router.huggingface.co/hf-inference/v1",
        default_model="meta-llama/Llama-3.2-3B-Instruct",
        auth="bearer",
        description="HF Inference API; free tier models.",
        free_tier=True,
        models=("meta-llama/Llama-3.2-3B-Instruct", "mistralai/Mistral-7B-Instruct-v0.3"),
    ),
    "tokenrouter": ProviderSpec(
        key="tokenrouter",
        label="TokenRouter",
        base_url="https://api.tokenrouter.io/v1",
        default_model="auto",
        auth="bearer",
        description="Gateway over 13 providers; budgets + failover.",
        models=("auto", "openai/gpt-5-mini", "anthropic/claude-sonnet-4-5"),
    ),
}

# Providers that work without an API key.
_KEYLESS_PROVIDERS = frozenset({"ollama"})

#: Explicit offline tier. When the
#: operator selects one of these, the choice is authoritative: the router
#: serves the deterministic echo path and must never silently promote
#: ambient real-provider credentials above it.
OFFLINE_PROVIDERS = frozenset({"echo", "none", ""})


def is_offline_provider(provider: str | None) -> bool:
    """True when ``provider`` names the explicit offline/deterministic tier."""
    return str(provider or "").strip().lower() in OFFLINE_PROVIDERS


def provider_spec(provider: str) -> ProviderSpec | None:
    return PROVIDERS.get(provider)


def surface_for_model(spec: ProviderSpec, model_id: str) -> str:
    """Resolve the wire surface a model must be addressed on.

    Provider default first, then the first matching per-model prefix override.
    Endpoint construction belongs to the adapter, never to call sites.
    """
    for prefix, surface in spec.model_surfaces:
        if model_id.startswith(prefix):
            return surface
    return spec.api_surface


#: OpenAI-parameters Groq documents as unsupported (they 400): logprobs,
#: logit_bias, top_logprobs, messages[].name, and any n != 1.
_GROQ_UNSUPPORTED_PARAMS = frozenset({"logprobs", "logit_bias", "top_logprobs"})


def _sanitize_payload(provider: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Strip provider-unsupported parameters before sending (pure transform).

    OpenAI compatibility does not mean every OpenAI parameter is accepted;
    each gateway documents its own exclusions. Stripping here keeps request
    normalization inside the adapter seam.
    """
    if provider == "groq":
        cleaned = {k: v for k, v in payload.items() if k not in _GROQ_UNSUPPORTED_PARAMS}
        n = cleaned.get("n")
        if n is not None and n != 1:
            cleaned.pop("n", None)
        messages = cleaned.get("messages")
        if isinstance(messages, list):
            cleaned["messages"] = [
                {k: v for k, v in m.items() if k != "name"} if isinstance(m, dict) else m
                for m in messages
            ]
        return cleaned
    return payload


def _sanitize_base_url(url: str | None) -> str:
    """Sanitize base URL by stripping trailing slashes and endpoint paths like /chat/completions."""
    if not url:
        return ""
    cleaned = url.strip().rstrip("/")
    while cleaned.endswith("/chat/completions"):
        cleaned = cleaned[: -len("/chat/completions")].rstrip("/")
    if "api.groq.com" in cleaned:
        if cleaned.endswith("/openai/v1"):
            return cleaned
        if cleaned.endswith("/openai"):
            return f"{cleaned}/v1"
        if cleaned.endswith("/v1"):
            return cleaned.replace("/v1", "/openai/v1")
        if cleaned in ("https://api.groq.com", "http://api.groq.com"):
            return f"{cleaned}/openai/v1"
    return cleaned


def provider_base_url(provider: str, settings: Settings) -> str | None:
    """Settings-overridable base URL for a provider (additive helper)."""
    spec = provider_spec(provider)
    if spec is None:
        return None
    overrides = {
        "anthropic": getattr(settings, "anthropic_base_url", None),
        "groq": settings.groq_base_url,
        "ollama": settings.ollama_base_url,
        "openrouter": settings.openrouter_base_url,
        "together": settings.together_base_url,
        "mistral": settings.mistral_base_url,
        "gemini": settings.gemini_base_url,
        "deepseek": settings.deepseek_base_url,
        "huggingface": settings.huggingface_base_url,
        "tokenrouter": settings.tokenrouter_base_url,
        "nim": settings.nim_base_url,
        "ollama_cloud": settings.ollama_cloud_base_url,
        "opencode": settings.opencode_base_url,
    }
    raw = overrides.get(provider, spec.base_url) or spec.base_url
    cleaned = _sanitize_base_url(raw)
    if provider == "anthropic":
        # Accept both base-URL conventions for Anthropic-compatible gateways:
        # the official SDK style (ANTHROPIC_BASE_URL=https://host, no /v1 —
        # endpoint paths are appended by the client) and Bob's spec style
        # (.../v1 baked into the base). Normalizing here keeps adapter
        # endpoint construction in one seam.
        if not cleaned.rstrip("/").endswith("/v1"):
            cleaned = cleaned.rstrip("/") + "/v1"
    return cleaned


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_none(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in kwargs.items() if v is not None}


def _extract_rate_limit_headers(headers: Any) -> dict[str, Any]:
    if not headers:
        return {}
    res: dict[str, Any] = {}
    for k, v in headers.items():
        lk = k.lower()
        if "ratelimit" in lk or lk == "retry-after":
            res[lk] = v
    return res


def _usage(data: dict[str, Any]) -> dict[str, Any]:
    usage = data.get("usage") or data.get("usage_") or {}
    return {
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "cached_tokens": int(usage.get("prompt_tokens_details", {}).get("cached_tokens") or 0),
    }


def _canonical_tool_calls(raw_items: Any) -> list[dict[str, Any]]:
    """Normalize provider-native structured tool calls into one carrier shape.

    Every adapter emits the same canonical list ``[{"id", "name", "arguments"}]``
    (``arguments`` as a JSON-encoded string, matching how the OpenAI
    ``function.arguments`` field arrives on the wire). Accepted inbound shapes:

    - OpenAI — ``{"id", "type": "function", "function": {"name", "arguments"}}``;
    - flat — ``{"id", "name", "arguments"}`` (and Gemini's ``args`` / Anthropic's
      ``input`` aliases, both of which carry a dict that we re-encode).

    Nameless entries are dropped; ``id`` is synthesized for providers that omit
    it (Gemini). The result rides the adapter result as ``"tool_calls"`` so the
    downstream execution lifecycle never branches per provider.
    """
    canonical: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items or []):
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if isinstance(function, dict):
            name = function.get("name")
            arguments: Any = function.get("arguments")
        else:
            name = item.get("name")
            arguments = item.get("arguments", item.get("args", item.get("input")))
        if not name:
            continue
        if isinstance(arguments, str):
            raw = arguments
        else:
            try:
                raw = json.dumps(arguments) if arguments is not None else "{}"
            except (TypeError, ValueError):
                raw = "{}"
        canonical.append(
            {
                "id": str(item.get("id") or f"call_native_{index + 1}"),
                "name": str(name),
                "arguments": raw,
            }
        )
    return canonical


def build_extra_headers(settings: Settings) -> dict[str, str]:
    """User-supplied headers applied to every provider call."""
    return settings.extra_headers


def _normalize_gemini_finish_reason(raw: Any) -> str:
    """Map Gemini-native ``finishReason`` enums onto the shared vocabulary.

    The runtime only branches on ``result.ok``/``output`` today, but every
    downstream consumer (logs, fallback ledger, Telegram footers) sees this
    field — Gemini-specific ``"STOP"``/``"SAFETY"`` strings must never leak
    past the adapter seam.
    """
    mapping = {
        "STOP": "stop",
        "MAX_TOKENS": "length",
        "SAFETY": "content_filter",
        "RECITATION": "content_filter",
        "BLOCKLIST": "content_filter",
        "PROHIBITED_CONTENT": "content_filter",
        "SPII": "content_filter",
        "MALFORMED_FUNCTION_CALL": "tool_calls",
        "OTHER": "stop",
        "FINISH_REASON_UNSPECIFIED": "stop",
    }
    return mapping.get(str(raw or "").upper(), str(raw or "stop").lower())


def _gemini_contents(messages: list[dict[str, Any]], fallback_prompt: str) -> list[dict[str, Any]]:
    """Convert OpenAI-style messages into Gemini-native ``contents``.

    Pure transformation: system prompts become a top-level instruction is
    *not* done here (the adapter has no system field to carry it today), so
    system text rides as a ``user`` turn — same convention the Gemini OpenAI
    compatibility layer applies for roleless callers.

    Tool results (``role: "tool"``) must become ``functionResponse`` parts or
    Gemini rejects the request; plain assistant text becomes a ``model`` turn
    (``assistant`` is an OpenAI role).
    """
    contents: list[dict[str, Any]] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user").lower()
        content = msg.get("content")
        text = content if isinstance(content, str) else ("" if content is None else str(content))
        if role == "tool" and msg.get("name"):
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": str(msg["name"]),
                                "response": {"result": text},
                            }
                        }
                    ],
                }
            )
        else:
            contents.append(
                {"role": "model" if role == "assistant" else "user", "parts": [{"text": text}]}
            )
    if not contents:
        contents = [{"role": "user", "parts": [{"text": fallback_prompt}]}]
    return contents


def _openai_tools_to_gemini(tools: Any) -> list[dict[str, Any]] | None:
    """Convert OpenAI ``tools`` (function defs) to Gemini functionDeclarations.

    Pure transform. Returns None when no usable function tools are given, so
    tool-incapable calls never receive a ``tools`` payload. A model that does
    not support tools must never receive tool definitions.
    """
    if not tools or not isinstance(tools, list):
        return None
    declarations: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not name:
            continue
        declarations.append(
            {
                "name": str(name),
                "description": str(fn.get("description") or ""),
                "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    if not declarations:
        return None
    return [{"functionDeclarations": declarations}]


def _gemini_tool_config(tool_choice: Any) -> dict[str, Any] | None:
    """Map OpenAI tool_choice onto Gemini tool_config (additive, best-effort)."""
    if tool_choice is None:
        return None
    if tool_choice == "required":
        return {"functionCallingConfig": {"mode": "ANY"}}
    if tool_choice == "none":
        return {"functionCallingConfig": {"mode": "NONE"}}
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function") if isinstance(tool_choice.get("function"), dict) else None
        allowed = [str(fn.get("name"))] if fn and fn.get("name") else None
        if allowed:
            return {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": allowed}}
        return {"functionCallingConfig": {"mode": "AUTO"}}
    return {"functionCallingConfig": {"mode": "AUTO"}}


# ---------------------------------------------------------------------------
# OpenAI Responses API (used by OpenCode Zen for GPT/Grok/Muse models)
# ---------------------------------------------------------------------------


def _chat_messages_to_responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert chat messages to Responses ``input`` items (pure transform)."""
    items: list[dict[str, Any]] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user").lower()
        content = msg.get("content")
        text = content if isinstance(content, str) else ("" if content is None else str(content))
        if role == "tool" and msg.get("name"):
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": str(msg.get("tool_call_id") or msg.get("id") or ""),
                    "output": text,
                }
            )
        elif role in ("system", "developer"):
            items.append({"type": "message", "role": "system", "content": text})
        elif role == "assistant":
            items.append({"type": "message", "role": "assistant", "content": text})
        else:
            items.append({"type": "message", "role": "user", "content": text})
    return items


def _parse_responses_payload(
    data: dict[str, Any], model_id: str, provider_key: str
) -> dict[str, Any]:
    """Parse an OpenAI Responses payload into Bob's adapter result shape."""
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    output = data.get("output") or []
    for item in output if isinstance(output, list) else []:
        if not isinstance(item, dict):
            continue
        itype = str(item.get("type") or "")
        if itype in ("message", "output_text"):
            content = item.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("text"):
                        inner = block.get("text")
                        text = inner.get("text") if isinstance(inner, dict) else inner
                        text_parts.append(str(text))
                    elif isinstance(block, str):
                        text_parts.append(block)
            elif isinstance(content, str):
                text_parts.append(content)
            elif isinstance(item.get("text"), str):
                text_parts.append(str(item["text"]))
        elif itype == "function_call":
            name = item.get("name")
            if not name:
                continue
            args = item.get("arguments")
            call_id = item.get("call_id") or item.get("id")
            tool_calls.append(
                {
                    "id": str(call_id or f"call_native_{len(tool_calls) + 1}"),
                    "name": str(name),
                    "arguments": args if isinstance(args, str) else json.dumps(args or {}),
                }
            )
    usage_raw = data.get("usage") or {}
    details = usage_raw.get("input_tokens_details") or {}
    usage = {
        "input_tokens": int(usage_raw.get("input_tokens") or 0),
        "output_tokens": int(usage_raw.get("output_tokens") or 0),
        "cached_tokens": int(details.get("cached_tokens") or 0),
    }
    status = str(data.get("status") or "")
    finish = "stop"
    if tool_calls:
        finish = "tool_calls"
    elif status in ("incomplete",) or data.get("incomplete_reason") == "max_output_tokens":
        finish = "length"
    return {
        "provider": provider_key,
        "model": model_id,
        "content": "".join(text_parts),
        "output": "".join(text_parts),
        "tool_calls": _canonical_tool_calls(tool_calls),
        "finish_reason": finish,
        "usage": usage,
        "request_id": str(data.get("id") or ""),
    }


# ---------------------------------------------------------------------------
# Adaptors
# ---------------------------------------------------------------------------


class OpenAICompatibleAdapter:
    """`/v1/chat/completions` client (OpenAI + all compatible providers)."""

    supports_streaming = True

    def __init__(
        self,
        spec: ProviderSpec,
        api_key: str | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: float = 120.0,
        app_site: str | None = None,
        app_name: str | None = None,
        supports_stream_usage: bool = False,
    ) -> None:
        self.spec = spec
        self.api_key = api_key
        self.extra_headers = extra_headers or {}
        self.timeout = timeout
        # OpenRouter attribution headers (interpolated from settings with
        # sensible defaults; never sent as literal placeholders).
        self.app_site = app_site or "https://localhost"
        self.app_name = app_name or "Bob Agent"
        # Whether the provider accepts OpenAI's
        # ``stream_options: {"include_usage": true}`` on SSE requests.
        # Groq/OpenRouter-compatible gateways reject unknown fields with
        # 400, so this is opt-in per provider (True today only for OpenAI).
        # When False the stream simply yields no usage chunk and the router
        # flags usage as estimated — never a failure.
        self.supports_stream_usage = supports_stream_usage

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.spec.auth == "bearer" and self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.spec.key == "openrouter":
            # Attribution headers are required by OpenRouter for proper
            # routing/analytics, including keyless free-tier calls.
            headers["HTTP-Referer"] = self.app_site
            headers["X-Title"] = self.app_name
        headers.update(self.extra_headers)
        return {k: v for k, v in headers.items() if v}

    def _endpoint(self, model_id: str) -> str:
        """Endpoint construction belongs to the adapter, never to call sites."""
        surface = surface_for_model(self.spec, model_id)
        suffix = "/responses" if surface == "responses" else "/chat/completions"
        return f"{self.spec.base_url}{suffix}"

    # -- conceptual adapter interface (OpenAI-style, additive) ----------
    provider_id: str = ""

    def validateCredentials(self) -> tuple[bool, str]:
        """Check credential presence without exposing the secret."""
        if self.spec.key in _KEYLESS_PROVIDERS:
            return True, "keyless provider"
        if self.api_key:
            return True, "configured"
        return False, f"missing API key for provider '{self.spec.key}'"

    def resolveModel(self, model_id: str) -> str:
        """Model IDs are never transformed — upstream expects exact IDs."""
        return (model_id or "").strip()

    def buildRequest(self, request: Any) -> dict[str, Any]:
        """Pure transform of a normalized request into the native payload."""
        try:
            from agent_system.services.llm_contract import NormalizedLLMRequest as _Req
        except ImportError:
            _Req = None  # type: ignore[assignment]
        if _Req is not None and isinstance(request, _Req):
            messages = [dict(m) for m in request.messages]
            kwargs: dict[str, Any] = {
                "temperature": request.temperature,
                "max_tokens": request.max_output_tokens,
            }
            if request.tools:
                kwargs["tools"] = [dict(t) for t in request.tools]
            if request.tool_choice is not None:
                kwargs["tool_choice"] = request.tool_choice
            if request.response_format is not None:
                kwargs["response_format"] = request.response_format
            return self._build_chat_payload(request.model, messages, **kwargs)
        if isinstance(request, dict):
            return dict(request)
        return {"model": str(request)}

    def _build_chat_payload(
        self, model_id: str, messages: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": model_id, "messages": messages}
        payload.update(
            _strip_none(
                {
                    "temperature": kwargs.get("temperature"),
                    "max_tokens": kwargs.get("max_tokens") or kwargs.get("max_completion_tokens"),
                    "top_p": kwargs.get("top_p"),
                    "stop": kwargs.get("stop"),
                    "stream": kwargs.get("stream", False),
                }
            )
        )
        extra = {k: v for k, v in kwargs.items() if k not in payload}
        payload.update(extra)
        return _sanitize_payload(self.spec.key, payload)

    def classifyError(self, exc: Any, **ctx: Any) -> Any:
        try:
            from agent_system.services.llm_contract import classify_provider_error as _classify
        except ImportError:
            return exc
        return _classify(exc, provider=self.spec.key, **ctx)

    def getCapabilities(self) -> Any:
        try:
            from agent_system.services.llm_catalog import capability_for as _cap_for
            from agent_system.services.llm_contract import ProviderCapabilities as _Caps
        except ImportError:
            return None
        # Capabilities are per-model; report the default model's flags.
        cap = _cap_for(self.spec.key, self.spec.default_model)
        return _Caps(
            provider=self.spec.key,
            supports_tools=cap.effective_supports_tools,
            supports_streaming=cap.supports_streaming,
            supports_structured_output=cap.effective_supports_structured,
            supports_vision=cap.effective_supports_vision,
            supports_parallel_tools=cap.supports_parallel_tools,
            api_surface=surface_for_model(self.spec, self.spec.default_model),
        )

    def invoke(self, model_id: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        # Route Responses-surface models to the correct wire protocol.
        if surface_for_model(self.spec, model_id) == "responses":
            return self._invoke_responses(model_id, prompt, **kwargs)
        messages = list(kwargs.pop("messages", None) or [])
        if not messages:
            messages = [{"role": "user", "content": prompt}]
        payload = self._build_chat_payload(model_id, messages, **kwargs)
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                self._endpoint(model_id),
                headers=self._headers(),
                json=payload,
            )
            rl_info = _extract_rate_limit_headers(resp.headers)
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                exc.rate_limit_info = rl_info  # type: ignore[attr-defined]
                raise
            data = resp.json()
        choices = data.get("choices") or []
        text = ""
        message: dict[str, Any] = {}
        finish_reason = None
        if choices:
            choice = choices[0]
            message = choice.get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                text = content
            elif content is None:
                text = ""
            else:
                text = str(content)
            finish_reason = choice.get("finish_reason")
        # finish_reason=tool_calls must surface structured calls, never text.
        if finish_reason == "tool_calls" and not message.get("tool_calls"):
            finish_reason = "stop"
        return {
            "provider": self.spec.key,
            "model": model_id,
            "content": text,
            "output": text,
            "tool_calls": _canonical_tool_calls(message.get("tool_calls")),
            "finish_reason": finish_reason or "stop",
            "usage": _usage(data),
            "request_id": str(data.get("id") or ""),
            "rate_limit_info": rl_info,
        }

    def _invoke_responses(self, model_id: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        """OpenAI Responses API invocation (OpenCode Zen GPT/Grok/Muse models)."""
        messages = list(kwargs.pop("messages", None) or [])
        if not messages:
            messages = [{"role": "user", "content": prompt}]
        payload: dict[str, Any] = {
            "model": model_id,
            "input": _chat_messages_to_responses_input(messages),
        }
        if kwargs.get("temperature") is not None:
            payload["temperature"] = kwargs.get("temperature")
        max_tok = kwargs.get("max_tokens") or kwargs.get("max_completion_tokens")
        if max_tok is not None:
            payload["max_output_tokens"] = max_tok
        if kwargs.get("tools"):
            payload["tools"] = kwargs.get("tools")
        if kwargs.get("tool_choice") is not None:
            payload["tool_choice"] = kwargs.get("tool_choice")
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                self._endpoint(model_id),
                headers=self._headers(),
                json=payload,
            )
            rl_info = _extract_rate_limit_headers(resp.headers)
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                exc.rate_limit_info = rl_info  # type: ignore[attr-defined]
                raise
            data = resp.json()
        result = _parse_responses_payload(data, model_id, self.spec.key)
        result["rate_limit_info"] = rl_info
        return result

    def stream(self, model_id: str, prompt: str, **kwargs: Any) -> Any:
        """Yield text deltas via SSE (``stream:true``); usage when provided.

        Returns ``(chunks, usage, tool_calls)``:
        - ``chunks``     iterator of ``str`` deltas;
        - ``usage``      dict populated from the terminal SSE chunk when the
          provider emits one (``stream_options.include_usage``, only
          requested when ``supports_stream_usage`` is True) — empty when
          the provider omits it, in which case the router flags usage as
          estimated, same as ``invoke``;
        - ``tool_calls`` list of canonical ``{"id", "name", "arguments"}`` calls
          assembled from the fragmented ``delta.tool_calls`` streaming chunks
          (``None`` when the provider streamed no structured calls).

        Responses-surface models stream ``response.output_text.delta`` /
        ``response.function_call_arguments.delta`` events instead.
        """
        if surface_for_model(self.spec, model_id) == "responses":
            return self._stream_responses(model_id, prompt, **kwargs)
        messages = list(kwargs.pop("messages", None) or [])
        if not messages:
            messages = [{"role": "user", "content": prompt}]
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "stream": True,
        }
        if self.supports_stream_usage:
            payload["stream_options"] = {"include_usage": True}
        payload.update(
            _strip_none(
                {
                    "temperature": kwargs.get("temperature"),
                    "max_tokens": kwargs.get("max_tokens") or kwargs.get("max_completion_tokens"),
                    "top_p": kwargs.get("top_p"),
                    "stop": kwargs.get("stop"),
                }
            )
        )
        extra = {k: v for k, v in kwargs.items() if k not in payload}
        payload.update(extra)
        payload = _sanitize_payload(self.spec.key, payload)
        usage: dict[str, Any] = {}
        #: Filled in-place by the generator's ``finally`` once the stream is
        #: exhausted, so the 3-tuple element reflects the assembled calls.
        tool_calls: list[dict[str, Any]] = []

        def _chunks() -> Any:
            assembled: dict[int, dict[str, str]] = {}
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    with client.stream(
                        "POST",
                        self._endpoint(model_id),
                        headers=self._headers(),
                        json=payload,
                    ) as resp:
                        resp.raise_for_status()
                        for line in resp.iter_lines():
                            if not line.startswith("data:"):
                                continue
                            data_raw = line[5:].strip()
                            if not data_raw or data_raw == "[DONE]":
                                continue
                            try:
                                import json as _sj

                                chunk = _sj.loads(data_raw)
                            except ValueError:
                                continue
                            if "usage" in chunk and isinstance(chunk["usage"], dict):
                                usage.update(_usage({"usage": chunk["usage"]}))
                            for choice in chunk.get("choices") or []:
                                delta = choice.get("delta") or {}
                                for item in delta.get("tool_calls") or []:
                                    if not isinstance(item, dict):
                                        continue
                                    index = int(item.get("index") or 0)
                                    slot = assembled.setdefault(
                                        index, {"id": "", "name": "", "arguments": ""}
                                    )
                                    if item.get("id"):
                                        slot["id"] = str(item["id"])
                                    function = item.get("function") or {}
                                    if function.get("name"):
                                        slot["name"] = str(function["name"])
                                    args_delta = function.get("arguments")
                                    if args_delta:
                                        slot["arguments"] += str(args_delta)
                                content = delta.get("content")
                                if content:
                                    yield str(content)
            finally:
                calls: list[dict[str, Any]] = []
                for index in sorted(assembled):
                    slot = assembled[index]
                    if not slot["name"]:
                        continue
                    calls.append(
                        {
                            "id": slot["id"] or f"call_native_{index + 1}",
                            "name": slot["name"],
                            "arguments": slot["arguments"] or "{}",
                        }
                    )
                tool_calls[:] = calls

        return _chunks(), usage, tool_calls

    def _stream_responses(self, model_id: str, prompt: str, **kwargs: Any) -> Any:
        """Stream the Responses API (SSE: output_text/function_call deltas)."""
        messages = list(kwargs.pop("messages", None) or [])
        if not messages:
            messages = [{"role": "user", "content": prompt}]
        payload: dict[str, Any] = {
            "model": model_id,
            "input": _chat_messages_to_responses_input(messages),
            "stream": True,
        }
        if kwargs.get("tools"):
            payload["tools"] = kwargs.get("tools")
        usage: dict[str, Any] = {}
        tool_calls: list[dict[str, Any]] = []

        def _chunks() -> Any:
            import json as _sj

            text_buf: list[str] = []
            func_name = ""
            func_call_id = ""
            func_args = ""
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    with client.stream(
                        "POST",
                        self._endpoint(model_id),
                        headers=self._headers(),
                        json=payload,
                    ) as resp:
                        resp.raise_for_status()
                        for line in resp.iter_lines():
                            if not line.startswith("data:"):
                                continue
                            data_raw = line[5:].strip()
                            if not data_raw or data_raw == "[DONE]":
                                continue
                            try:
                                evt = _sj.loads(data_raw)
                            except ValueError:
                                continue
                            etype = str(evt.get("type") or "")
                            if etype in ("response.output_text.delta", "output_text.delta"):
                                delta = evt.get("delta") or evt.get("text") or ""
                                if delta:
                                    text_buf.append(str(delta))
                                    yield str(delta)
                            elif etype in (
                                "response.function_call_arguments.delta",
                                "function_call_arguments.delta",
                            ):
                                delta = evt.get("delta") or evt.get("arguments") or ""
                                if delta:
                                    func_args += str(delta)
                            elif etype in (
                                "response.output_item.added",
                                "output_item.added",
                            ):
                                item = evt.get("item") or {}
                                is_fn = isinstance(item, dict) and (
                                    item.get("type") == "function_call"
                                )
                                if is_fn:
                                    func_name = str(item.get("name") or func_name)
                                    call_id = item.get("call_id") or item.get("id")
                                    func_call_id = str(call_id or func_call_id)
                            elif etype == "response.completed":
                                response = evt.get("response") or {}
                                out = response.get("output") or []
                                for item in out if isinstance(out, list) else []:
                                    is_fn = isinstance(item, dict) and (
                                        item.get("type") == "function_call"
                                    )
                                    if is_fn:
                                        func_name = str(item.get("name") or func_name)
                                        call_id = item.get("call_id") or item.get("id")
                                        func_call_id = str(call_id or func_call_id)
                                        args = item.get("arguments")
                                        if args and not func_args:
                                            func_args = (
                                                args if isinstance(args, str) else _sj.dumps(args)
                                            )
                                usage_raw = response.get("usage") or evt.get("usage") or {}
                                if isinstance(usage_raw, dict) and usage_raw:
                                    usage.update(_usage({"usage": usage_raw}))
            finally:
                if func_name:
                    tool_calls[:] = _canonical_tool_calls(
                        [
                            {
                                "id": func_call_id or "call_native_1",
                                "name": func_name,
                                "arguments": func_args or "{}",
                            }
                        ]
                    )

        return _chunks(), usage, tool_calls


class GeminiAdapter:
    """Google Gemini native `:generateContent` (uses `x-goog-api-key`)."""

    supports_streaming = True

    def __init__(
        self,
        spec: ProviderSpec,
        api_key: str | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.spec = spec
        self.api_key = api_key
        self.extra_headers = extra_headers or {}
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            # x-goog-api-key header is preferred; query param as fallback.
            headers["x-goog-api-key"] = self.api_key
        headers.update(self.extra_headers)
        return headers

    def validateCredentials(self) -> tuple[bool, str]:
        if self.api_key:
            return True, "configured"
        return False, "missing API key for provider 'gemini' (GEMINI_API_KEY/GOOGLE_API_KEY)"

    def resolveModel(self, model_id: str) -> str:
        return (model_id or "").strip()

    def classifyError(self, exc: Any, **ctx: Any) -> Any:
        try:
            from agent_system.services.llm_contract import classify_provider_error as _classify
        except ImportError:
            return exc
        return _classify(exc, provider=self.spec.key, **ctx)

    def invoke(self, model_id: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{self.spec.base_url}/models/{model_id}:generateContent"
        headers = self._headers()
        messages = list(kwargs.pop("messages", None) or [])
        payload: dict[str, Any] = {"contents": _gemini_contents(messages, prompt)}
        generation_config: dict[str, Any] = {}
        temperature = kwargs.get("temperature")
        if temperature is not None:
            generation_config["temperature"] = temperature
        max_tok = kwargs.get("max_tokens") or kwargs.get("max_completion_tokens")
        if max_tok is not None:
            try:
                generation_config["maxOutputTokens"] = int(max_tok)
            except (TypeError, ValueError):
                pass
        if generation_config:
            payload["generationConfig"] = generation_config
        # Native function calling: translate OpenAI tools, never drop them.
        tools = _openai_tools_to_gemini(kwargs.get("tools"))
        if tools is not None:
            payload["tools"] = tools
            tool_config = _gemini_tool_config(kwargs.get("tool_choice"))
            if tool_config is not None:
                payload["toolConfig"] = tool_config
        # Structured output: map response_format json_schema to Gemini config.
        response_format = kwargs.get("response_format")
        if isinstance(response_format, dict):
            nested = response_format.get("json_schema", {}) or {}
            schema = nested.get("schema") or response_format.get("schema")
            if isinstance(schema, dict):
                payload.setdefault("generationConfig", {})
                payload["generationConfig"]["responseMimeType"] = "application/json"
                payload["generationConfig"]["responseSchema"] = schema
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        candidates = data.get("candidates") or []
        text = ""
        function_calls: list[dict[str, Any]] = []
        finish_reason = None
        if candidates:
            cand = candidates[0]
            finish_reason = _normalize_gemini_finish_reason(cand.get("finishReason"))
            parts = (cand.get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts)
            function_calls = [
                p.get("functionCall") for p in parts if isinstance(p.get("functionCall"), dict)
            ]
        usage_meta = data.get("usageMetadata") or {}
        return {
            "provider": self.spec.key,
            "model": model_id,
            "content": text,
            "output": text,
            "tool_calls": _canonical_tool_calls(function_calls),
            "finish_reason": finish_reason or "stop",
            "usage": {
                "input_tokens": int(usage_meta.get("promptTokenCount") or 0),
                "output_tokens": int(usage_meta.get("candidatesTokenCount") or 0),
                "cached_tokens": 0,
            },
            "request_id": "",
        }

    def stream(self, model_id: str, prompt: str, **kwargs: Any) -> Any:
        """Yield text deltas via `:streamGenerateContent?alt=sse`.

        Same ``(chunks, usage, tool_calls)`` contract as
        ``OpenAICompatibleAdapter.stream`` — each SSE ``data:`` line is one
        ``GenerateContentResponse`` JSON object; text parts are yielded and
        ``functionCall`` parts are collected, then normalized into the
        canonical carrier in the generator's ``finally``.
        """
        url = f"{self.spec.base_url}/models/{model_id}:streamGenerateContent?alt=sse"
        headers = self._headers()
        messages = list(kwargs.pop("messages", None) or [])
        payload: dict[str, Any] = {"contents": _gemini_contents(messages, prompt)}
        generation_config: dict[str, Any] = {}
        temperature = kwargs.get("temperature")
        if temperature is not None:
            generation_config["temperature"] = temperature
        max_tok = kwargs.get("max_tokens") or kwargs.get("max_completion_tokens")
        if max_tok is not None:
            try:
                generation_config["maxOutputTokens"] = int(max_tok)
            except (TypeError, ValueError):
                pass
        if generation_config:
            payload["generationConfig"] = generation_config
        tools = _openai_tools_to_gemini(kwargs.get("tools"))
        if tools is not None:
            payload["tools"] = tools
            tool_config = _gemini_tool_config(kwargs.get("tool_choice"))
            if tool_config is not None:
                payload["toolConfig"] = tool_config
        usage: dict[str, Any] = {}
        #: Filled in-place by the generator's ``finally`` once the stream is
        #: exhausted, so the 3-tuple element reflects the assembled calls.
        tool_calls: list[dict[str, Any]] = []

        def _chunks() -> Any:
            import json as _sj

            raw_calls: list[dict[str, Any]] = []
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    with client.stream(
                        "POST",
                        url,
                        headers=headers,
                        json=payload,
                    ) as resp:
                        resp.raise_for_status()
                        for line in resp.iter_lines():
                            if not line.startswith("data:"):
                                continue
                            data_raw = line[5:].strip()
                            if not data_raw or data_raw == "[DONE]":
                                continue
                            try:
                                evt = _sj.loads(data_raw)
                            except ValueError:
                                continue
                            meta = evt.get("usageMetadata") or {}
                            if meta.get("promptTokenCount"):
                                usage["input_tokens"] = int(meta["promptTokenCount"])
                            if meta.get("candidatesTokenCount"):
                                usage["output_tokens"] = int(meta["candidatesTokenCount"])
                            for cand in evt.get("candidates") or []:
                                for part in (cand.get("content") or {}).get("parts") or []:
                                    if not isinstance(part, dict):
                                        continue
                                    if part.get("text"):
                                        yield str(part["text"])
                                    if isinstance(part.get("functionCall"), dict):
                                        raw_calls.append(part["functionCall"])
            finally:
                tool_calls[:] = _canonical_tool_calls(raw_calls)

        return _chunks(), usage, tool_calls


class AnthropicAdapter:
    """Anthropic native `/v1/messages` (x-api-key + anthropic-version)."""

    supports_streaming = True

    def __init__(
        self,
        spec: ProviderSpec,
        api_key: str | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.spec = spec
        self.api_key = api_key
        self.extra_headers = extra_headers or {}
        self.timeout = timeout

    def invoke(self, model_id: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        messages = list(kwargs.pop("messages", None) or [])
        if not messages:
            messages = [{"role": "user", "content": prompt}]
        payload: dict[str, Any] = {
            "model": model_id,
            "max_tokens": int(kwargs.get("max_tokens") or 1024),
            "messages": messages,
        }
        if kwargs.get("system"):
            payload["system"] = kwargs["system"]
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        headers.update(self.extra_headers)
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(f"{self.spec.base_url}/messages", headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        usage = data.get("usage") or {}
        return {
            "provider": self.spec.key,
            "model": model_id,
            "content": text,
            "output": text,
            "tool_calls": _canonical_tool_calls(
                [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
            ),
            "finish_reason": str(data.get("stop_reason") or "stop"),
            "usage": {
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "cached_tokens": int(usage.get("cache_read_input_tokens") or 0),
            },
            "request_id": str(data.get("id") or ""),
        }

    def stream(self, model_id: str, prompt: str, **kwargs: Any) -> Any:
        """Yield text deltas via Anthropic SSE; usage from message deltas.

        Same ``(chunks, usage, tool_calls)`` contract as
        ``OpenAICompatibleAdapter.stream`` — ``tool_use`` blocks are assembled
        from ``content_block_start`` / ``content_block_delta``
        (``input_json_delta.partial_json`` fragments) / ``content_block_stop``.
        """
        messages = list(kwargs.pop("messages", None) or [])
        if not messages:
            messages = [{"role": "user", "content": prompt}]
        payload: dict[str, Any] = {
            "model": model_id,
            "max_tokens": int(kwargs.get("max_tokens") or 1024),
            "messages": messages,
            "stream": True,
        }
        if kwargs.get("system"):
            payload["system"] = kwargs["system"]
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "Accept": "text/event-stream",
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        headers.update(self.extra_headers)
        # Populated as chunks are consumed; stays empty when the provider
        # omits usage (the router then flags usage as estimated).
        usage: dict[str, Any] = {}
        #: Filled in-place by the generator's ``finally`` once the stream is
        #: exhausted, so the 3-tuple element reflects the assembled calls.
        tool_calls: list[dict[str, Any]] = []

        def _chunks() -> Any:
            import json as _sj

            assembled: dict[int, dict[str, str]] = {}
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    with client.stream(
                        "POST",
                        f"{self.spec.base_url}/messages",
                        headers=headers,
                        json=payload,
                    ) as resp:
                        resp.raise_for_status()
                        event_name = ""
                        for line in resp.iter_lines():
                            if line.startswith("event:"):
                                event_name = line[6:].strip()
                                continue
                            if not line.startswith("data:"):
                                continue
                            data_raw = line[5:].strip()
                            if not data_raw:
                                continue
                            try:
                                data_evt = _sj.loads(data_raw)
                            except ValueError:
                                continue
                            if event_name == "content_block_start":
                                block = data_evt.get("content_block") or {}
                                if block.get("type") == "tool_use":
                                    index = int(data_evt.get("index") or 0)
                                    assembled[index] = {
                                        "id": str(block.get("id") or ""),
                                        "name": str(block.get("name") or ""),
                                        "arguments": "",
                                    }
                            elif event_name == "content_block_delta":
                                delta = data_evt.get("delta") or {}
                                if delta.get("type") == "text_delta" and delta.get("text"):
                                    yield str(delta["text"])
                                elif delta.get("type") == "input_json_delta":
                                    partial = delta.get("partial_json")
                                    if partial:
                                        index = int(data_evt.get("index") or 0)
                                        slot = assembled.setdefault(
                                            index, {"id": "", "name": "", "arguments": ""}
                                        )
                                        slot["arguments"] += str(partial)
                            elif event_name in ("message_start", "message_delta"):
                                msg_usage = (
                                    data_evt.get("message", {}).get("usage")
                                    or data_evt.get("usage")
                                    or {}
                                )
                                # Each SSE message only carries *some* counters
                                # (message_start: input; message_delta: output);
                                # update in place so a later event never
                                # clobbers a counter it did not include.
                                if msg_usage.get("input_tokens"):
                                    usage["input_tokens"] = int(msg_usage["input_tokens"])
                                if msg_usage.get("output_tokens"):
                                    usage["output_tokens"] = int(msg_usage["output_tokens"])
                                if msg_usage.get("cache_read_input_tokens"):
                                    usage["cached_tokens"] = int(
                                        msg_usage["cache_read_input_tokens"]
                                    )
            finally:
                calls: list[dict[str, Any]] = []
                for index in sorted(assembled):
                    slot = assembled[index]
                    if not slot["name"]:
                        continue
                    calls.append(
                        {
                            "id": slot["id"] or f"call_native_{index + 1}",
                            "name": slot["name"],
                            "arguments": slot["arguments"] or "{}",
                        }
                    )
                tool_calls[:] = calls

        return _chunks(), usage, tool_calls


ADAPTER_CLASSES: dict[str, type[Any]] = {
    "openai": OpenAICompatibleAdapter,
    "groq": OpenAICompatibleAdapter,
    "ollama": OpenAICompatibleAdapter,
    "openrouter": OpenAICompatibleAdapter,
    "together": OpenAICompatibleAdapter,
    "mistral": OpenAICompatibleAdapter,
    "deepseek": OpenAICompatibleAdapter,
    "huggingface": OpenAICompatibleAdapter,
    "tokenrouter": OpenAICompatibleAdapter,
    "nim": OpenAICompatibleAdapter,
    "ollama_cloud": OpenAICompatibleAdapter,
    "opencode": OpenAICompatibleAdapter,
    "gemini": GeminiAdapter,
    "anthropic": AnthropicAdapter,
}


#: Official Gemini OpenAI-compatible base (ai.google.dev/gemini-api/docs/openai).
GEMINI_OPENAI_COMPAT_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"


def build_adapter(provider: str, settings: Settings, api_key: str | None = None) -> Any | None:
    """Construct the right adapter for a provider (None if unsupported).

    Gemini supports two transports: native ``:generateContent`` (default)
    and the OpenAI-compatible endpoint
    ``https://generativelanguage.googleapis.com/v1beta/openai/``. When the
    configured ``gemini_base_url`` points at the ``/openai`` compat layer,
    Bob uses the OpenAI-compatible adapter (Bearer auth) so the shared
    chat-completions path benefits apply; otherwise native is used.
    Endpoint construction always belongs to the adapter.
    """
    from dataclasses import replace

    spec = provider_spec(provider)
    cls = ADAPTER_CLASSES.get(provider)
    if spec is None or cls is None:
        return None
    base_url = _sanitize_base_url(provider_base_url(provider, settings) or spec.base_url)
    if provider == "gemini" and "/openai" in (base_url or ""):
        # OpenAI-compat transport for Gemini: Bearer auth + chat/completions.
        compat_spec = replace(spec, base_url=base_url.rstrip("/"), auth="bearer")
        extra = build_extra_headers(settings)
        return OpenAICompatibleAdapter(
            compat_spec,
            api_key=api_key,
            extra_headers=extra,
            app_site=settings.openrouter_site_url or "https://localhost",
            app_name=settings.openrouter_app_name or "Bob Agent",
            supports_stream_usage=False,
        )
    if base_url != spec.base_url:
        spec = replace(spec, base_url=base_url)
    extra = build_extra_headers(settings)
    if provider == "openrouter":
        extra.setdefault("HTTP-Referer", settings.openrouter_site_url)
        extra.setdefault("X-Title", settings.openrouter_app_name)
    if cls is OpenAICompatibleAdapter:
        return cls(
            spec,
            api_key=api_key,
            extra_headers=extra,
            app_site=settings.openrouter_site_url or "https://localhost",
            app_name=settings.openrouter_app_name or "Bob Agent",
            # Only OpenAI itself is known to accept
            # ``stream_options.include_usage``; every other OpenAI-compatible
            # gateway (Groq, OpenRouter, proxies) may 400 on it.
            supports_stream_usage=(provider == "openai"),
        )
    return cls(spec, api_key=api_key, extra_headers=extra)


def configured_providers(settings: Settings) -> list[dict[str, Any]]:
    """List providers with their configured state (no keys exposed)."""
    out: list[dict[str, Any]] = []
    for key, spec in PROVIDERS.items():
        if key in _KEYLESS_PROVIDERS:
            out.append(
                {
                    "key": key,
                    "label": spec.label,
                    "configured": True,
                    "needs_key": False,
                    "base_url": spec.base_url,
                    "default_model": spec.default_model,
                    "free_tier": spec.free_tier,
                    "description": spec.description,
                }
            )
            continue
        key_present = bool(settings.provider_api_key(key))
        out.append(
            {
                "key": key,
                "label": spec.label,
                "configured": key_present,
                "needs_key": True,
                "base_url": provider_base_url(key, settings) or spec.base_url,
                "default_model": spec.default_model,
                "free_tier": spec.free_tier,
                "description": spec.description,
            }
        )
    return out


def credential_summary(settings: Settings) -> dict[str, str]:
    """Diagnostic credential report: configured/missing only, never secrets.

    Covers GOOGLE_API_KEY/GEMINI_API_KEY, GROQ_API_KEY, OPENROUTER_API_KEY
    and OPENCODE_API_KEY (or the correct OpenCode credential mechanism).
    """
    labels = {
        "gemini": "Google",
        "groq": "Groq",
        "openrouter": "OpenRouter",
        "opencode": "OpenCode",
    }
    out: dict[str, str] = {}
    for key, label in labels.items():
        try:
            out[label] = "configured" if settings.provider_api_key(key) else "missing"
        except Exception:
            out[label] = "missing"
    return out


def diagnose_provider(
    settings: Settings,
    provider: str,
    model: str | None = None,
    prompt: str = "ping",
    include_tools: bool = True,
) -> dict[str, Any]:
    """Staged provider self-test: identify the exact failing stage.

    Stages: credentials → endpoint → model availability → completion →
    streaming → tool-call (when supported). Each stage reports PASS/FAIL/
    SKIP with an error message that never contains the API key. Far more
    useful than a single "provider failed".
    """
    import time as _time

    spec = provider_spec(provider)
    if spec is None:
        return {"ok": False, "provider": provider, "error": "unknown provider", "stages": {}}
    stages: dict[str, dict[str, Any]] = {}
    model_id = model or spec.default_model
    ok_all = True

    # 1. Credentials — never prints the secret.
    api_key = settings.provider_api_key(provider)
    keyless = provider in _KEYLESS_PROVIDERS
    cred_ok = keyless or bool(api_key)
    stages["credentials"] = {
        "status": "PASS" if cred_ok else "FAIL",
        "configured": bool(api_key),
        "keyless": keyless,
    }
    ok_all = ok_all and cred_ok
    if not ok_all:
        stages["endpoint"] = {"status": "SKIP"}
        stages["model"] = {"status": "SKIP"}
        stages["completion"] = {"status": "SKIP"}
        stages["streaming"] = {"status": "SKIP"}
        return {"ok": False, "provider": provider, "model": model_id, "stages": stages}

    adapter = build_adapter(provider, settings, api_key)
    if adapter is None:
        stages["endpoint"] = {"status": "FAIL", "error": "unsupported provider"}
        return {"ok": False, "provider": provider, "model": model_id, "stages": stages}
    effective_base = provider_base_url(provider, settings) or spec.base_url
    stages["endpoint"] = {"status": "PASS", "base_url": effective_base}
    # Model availability: IDs must remain exactly what upstream expects.
    from agent_system.services.llm_catalog import capability_for as _cap_for

    _cap = _cap_for(provider, model_id)
    stages["model"] = {
        "status": "PASS",
        "model_id": model_id,
        "known": bool(_cap.provider == provider and _cap.model_id == model_id),
        "api_surface": surface_for_model(spec, model_id),
        "supports_tools": _cap.effective_supports_tools,
        "supports_streaming": _cap.supports_streaming,
    }

    started = _time.monotonic()
    try:
        result = adapter.invoke(model_id, prompt)
        latency_ms = int((_time.monotonic() - started) * 1000)
        output = str(result.get("output") or "")
        if not output:
            raise RuntimeError("empty completion (no output text)")
        stages["completion"] = {
            "status": "PASS",
            "latency_ms": latency_ms,
            "output_excerpt": output[:120],
            "usage": result.get("usage"),
        }
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if api_key and api_key in message:
            message = message.replace(api_key, "***")
        stages["completion"] = {
            "status": "FAIL",
            "error": f"{type(exc).__name__}: {message[:300]}",
        }
        stages["streaming"] = {"status": "SKIP"}
        stages["tools"] = {"status": "SKIP"}
        return {"ok": False, "provider": provider, "model": model_id, "stages": stages}

    # Streaming probe (always, adapters without SSE degrade to one chunk).
    try:
        chunks, _usage, _calls = adapter.stream(model_id, prompt)
        text = "".join(str(c) for c in chunks)
        stages["streaming"] = {
            "status": "PASS",
            "chars": len(text),
        }
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if api_key and api_key in message:
            message = message.replace(api_key, "***")
        stages["streaming"] = {
            "status": "FAIL",
            "error": f"{type(exc).__name__}: {message[:300]}",
        }
        ok_all = False

    # Tool-call probe: only for models the catalog marks tool-capable.
    # Sends a real tool definition (get_test_value) so native function
    # calling is exercised; fenced-text answers do not count as PASS.
    if include_tools:
        from agent_system.services.llm_catalog import capability_for

        capability = capability_for(provider, model_id)
        if not capability.effective_supports_tools:
            stages["tools"] = {"status": "SKIP", "reason": "model not tool-capable"}
        else:
            tool_prompt = (
                "Call the tool get_test_value with arguments {} exactly, then stop. "
                "Respond with only the tool call."
            )
            _test_tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "get_test_value",
                        "description": "Return a test value for diagnostics.",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]
            try:
                t_result = adapter.invoke(model_id, tool_prompt, tools=_test_tools)
                calls = t_result.get("tool_calls") or []
                if calls:
                    stages["tools"] = {
                        "status": "PASS",
                        "tool_names": [c.get("name") for c in calls][:5],
                    }
                else:
                    stages["tools"] = {
                        "status": "FAIL",
                        "error": "no structured tool call returned (model may have "
                        "answered in text; check tool support for this model)",
                    }
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                if api_key and api_key in message:
                    message = message.replace(api_key, "***")
                stages["tools"] = {
                    "status": "FAIL",
                    "error": f"{type(exc).__name__}: {message[:300]}",
                }
            if stages["tools"].get("status") == "FAIL":
                ok_all = False

    return {"ok": ok_all, "provider": provider, "model": model_id, "stages": stages}


def test_provider(
    settings: Settings, provider: str, model: str | None = None, prompt: str = "ping"
) -> dict[str, Any]:
    """Back-compat single-shot probe (delegates to the staged diagnostics)."""
    result = diagnose_provider(settings, provider, model=model, prompt=prompt)
    stages = result.get("stages") or {}
    completion = stages.get("completion") or {}
    if result.get("ok"):
        return {
            "ok": True,
            "provider": provider,
            "model": result.get("model"),
            "output_excerpt": str(completion.get("output_excerpt") or "")[:200],
            "usage": completion.get("usage"),
        }
    error = (
        result.get("error") or completion.get("error") or stages.get("credentials", {}).get("error")
    )
    return {"ok": False, "provider": provider, "error": error or "diagnostics failed"}


def serialize_pricing(pricing: Any) -> dict[str, Any]:
    """Export PricingRegistry models as JSON-safe structure."""
    return {
        m.model_id: {
            "provider": m.provider,
            "input_cost_per_1m": m.input_cost_per_1m,
            "output_cost_per_1m": m.output_cost_per_1m,
            "context_window": m.context_window,
            "capabilities": list(m.capabilities),
        }
        for m in pricing.all()
    }


# ---------------------------------------------------------------------------
# Pricing registry + router assembly
# ---------------------------------------------------------------------------


def build_pricing(settings: Settings | None = None) -> Any:
    """PricingRegistry with honest costs.

    Free-tier providers register at $0.00 (real cost). Billable providers are
    left unregistered so the router flags their cost as *estimated* rather
    than fabricating cheap numbers.
    """
    from agent_system.services.model_router import ModelInfo, PricingRegistry

    registry = PricingRegistry()
    _ = settings
    for spec in PROVIDERS.values():
        if not spec.free_tier:
            continue
        for model in spec.models:
            registry.register(
                ModelInfo(
                    model_id=model,
                    provider=spec.key,
                    input_cost_per_1m=0.0,
                    output_cost_per_1m=0.0,
                    context_window=128_000,
                    capabilities=("general",),
                )
            )
    return registry


def default_model_id(settings: Settings) -> str:
    """Resolve the effective default model id for the configured provider."""
    if settings.default_model:
        return settings.default_model
    static = provider_spec(settings.default_provider)
    return static.default_model if static else "echo-default"


def routing_index(settings: Settings | None = None) -> dict[str, str]:
    """model_id -> owning provider for every model Bob can address.

    Covers each provider spec's catalogued models plus the configured
    Ollama Cloud role models, so routing never depends on a model being
    priced (pricing stays a cost fact, routing is an ownership fact).
    """
    index: dict[str, str] = {}
    for key, spec in PROVIDERS.items():
        for model in spec.models:
            index.setdefault(model, key)
        if spec.default_model:
            index.setdefault(spec.default_model, key)
    if settings is not None:
        try:
            for model in settings.ollama_model_roles.values():
                index[model] = "ollama_cloud"
        except Exception:
            pass
    return index


def build_model_router(
    event_bus: Any,
    settings: Settings,
    provider_names: list[str] | None = None,
    skill_manager: Any | None = None,
    soul_text: str | None = None,
) -> Any:
    """Assemble a fully-configured ModelRouter.

    Registers adapters only for configured providers (or the explicitly
    provided ``provider_names``). Sets a default routing rule so agents can
    call ``router.registry.select("default")`` / ``select("*")``. When a
    ``skill_manager`` is given, ``router.invoke(..., skills=[...])`` injects
    skill instructions into the prompt; ``soul_text`` is prepended as the
    ``<identity>`` block on every call. If ``soul_text`` is None, it is
    automatically loaded via ``load_soul()``.
    """
    from agent_system.services.model_router import (
        ModelRegistry,
        ModelRouter,
        SelectionRule,
    )

    if soul_text is None:
        try:
            from agent_system.services.soul import load_soul

            _, loaded_soul = load_soul(getattr(settings, "soul_path", "") or None)
            soul_text = loaded_soul or None
        except Exception:
            pass

    router = ModelRouter(
        event_bus,
        pricing=build_pricing(settings),
        registry=ModelRegistry(),
        default_provider=settings.default_provider,
        default_model=default_model_id(settings),
        skill_manager=skill_manager,
        soul_text=soul_text,
        circuit_breaker_threshold=int(getattr(settings, "circuit_breaker_threshold", 5) or 5),
        circuit_breaker_cooldown_seconds=float(
            getattr(settings, "circuit_breaker_cooldown_seconds", 60) or 60
        ),
        daily_budget_usd=float(getattr(settings, "daily_budget_usd", 10.0) or 10.0),
        settings=settings,
    )
    if provider_names is None:
        provider_names = [
            p["key"]
            for p in configured_providers(settings)
            if p["configured"] or p["key"] in _KEYLESS_PROVIDERS
        ]
    for name in provider_names:
        key = settings.provider_api_key(name) if name not in _KEYLESS_PROVIDERS else None
        adapter = build_adapter(name, settings, key)
        if adapter is not None:
            router.register_adapter(name, adapter)
    # Routing ownership index: the pricing registry deliberately leaves
    # billable models unregistered, so every model id Bob may address must
    # also be indexed against its owning provider — otherwise a configured
    # Ollama Cloud role model (or any non-"free" model) resolves to
    # provider="unknown" and fails with "no adapter registered".
    router.register_provider_index(routing_index(settings))

    # Explicit offline tier wins: an
    # operator-selected echo/none/empty provider is authoritative. Serve the
    # deterministic path even when ambient real credentials exist — never
    # silently reroute offline work onto the network.
    if is_offline_provider(router.default_provider):
        from agent_system.services.model_router import EchoProvider, ModelInfo

        if router._adapter_for("echo-default") is None:  # noqa: SLF001
            router.register_adapter("echo", EchoProvider())
        if router.pricing.get("echo-default") is None:
            router.pricing.register(
                ModelInfo(
                    model_id="echo-default",
                    provider="echo",
                    input_cost_per_1m=0.0,
                    output_cost_per_1m=0.0,
                )
            )
        router.default_provider = "echo"
        router.default_model = settings.default_model or "echo-default"
    # If the default provider adapter is not registered or is unconfigured ollama without
    # a key, fall back to echo
    elif router.default_provider not in router._adapters or (
        router.default_provider == "ollama" and not settings.provider_api_key("ollama")
    ):
        # Auto-selection across *routable* providers only: a quarantined
        # provider must never become the default merely by registering first.
        routable: list[str] = []
        try:
            from agent_system.services.provider_health import GLOBAL_HEALTH_TRACKER

            for k in router._adapters.keys():
                if k == "ollama":
                    continue
                spec_k = provider_spec(k)
                model_k = spec_k.default_model if spec_k else ""
                if GLOBAL_HEALTH_TRACKER.is_routable(k, model_k):
                    routable.append(k)
        except Exception:
            routable = []
        registered_non_ollama = routable or [k for k in router._adapters.keys() if k != "ollama"]
        if registered_non_ollama:
            import logging as _logging

            first_avail = registered_non_ollama[0]
            _logging.getLogger(__name__).warning(
                "[LLM] provider_selected default=%s unusable; auto-selected=%s",
                router.default_provider,
                first_avail,
            )
            router.default_provider = first_avail
            spec = provider_spec(first_avail)
            router.default_model = spec.default_model if spec else "echo-default"
        else:
            from agent_system.services.model_router import EchoProvider, ModelInfo

            router.register_adapter("echo", EchoProvider())
            router.default_provider = "echo"
            router.default_model = "echo-default"
            router.pricing.register(
                ModelInfo(
                    model_id="echo-default",
                    provider="echo",
                    input_cost_per_1m=0.0,
                    output_cost_per_1m=0.0,
                )
            )

    # Even if the default provider is registered, demote it if health tracking
    # marks it as blocked/unreachable (e.g. groq org-level model block -> 403).
    else:
        try:
            from agent_system.services.provider_health import GLOBAL_HEALTH_TRACKER

            default_spec = provider_spec(router.default_provider)
            default_provider_model = default_spec.default_model if default_spec else ""
            # NOTE: do not name this `default_model_id` — that is the
            # module-level function used above; a local of the same name
            # shadowed it and raised UnboundLocalError on EVERY router build
            # (i.e. every chat message and agent task failed with
            # "cannot access local variable 'default_model_id'").
            if GLOBAL_HEALTH_TRACKER is not None and not GLOBAL_HEALTH_TRACKER.is_routable(
                router.default_provider, default_provider_model
            ):
                # Find first routable non-ollama provider
                routable = []
                for k in router._adapters.keys():
                    if k == "ollama":
                        continue
                    spec_k = provider_spec(k)
                    model_k = spec_k.default_model if spec_k else ""
                    if GLOBAL_HEALTH_TRACKER.is_routable(k, model_k):
                        routable.append(k)
                if routable:
                    first_avail = routable[0]
                    import logging as _logging

                    _logging.getLogger(__name__).warning(
                        "[LLM] provider_selected default=%s blocked; auto-selected=%s",
                        router.default_provider,
                        first_avail,
                    )
                    router.default_provider = first_avail
                    spec = provider_spec(first_avail)
                    router.default_model = spec.default_model if spec else "echo-default"
        except Exception:
            pass

    router.registry.set_rule(
        SelectionRule(task_type="default", primary=router.default_model, fallback=None)
    )
    router.registry.set_rule(
        SelectionRule(task_type="*", primary=router.default_model, fallback=None)
    )
    return router
