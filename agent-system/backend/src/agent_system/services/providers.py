"""Real LLM provider adapters (optional; system boots without any).

Implements the `ProviderAdapter` interface from `model_router.py` for the
providers the setup wizard offers. All adapters are pure HTTP clients (httpx,
already a dependency) — no heavy SDKs.

Supported transports:

- **OpenAI-compatible** (`/v1/chat/completions`): Groq, Ollama, OpenRouter,
  Together, Mistral, DeepSeek, HuggingFace router, FreeLLMAPI, TokenRouter,
  and OpenAI itself.
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
        default_model="llama-3.3-70b-versatile",
        auth="bearer",
        description="Ultra-fast inference; free tier available.",
        free_tier=True,
        models=("llama-3.3-70b-versatile", "llama-3.1-8b-instant", "llama3-8b-8192"),
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
        default_model="meta-llama/llama-3.3-70b-instruct:free",
        auth="bearer",
        description="Aggregator with many free models.",
        free_tier=True,
        models=(
            "meta-llama/llama-3.3-70b-instruct:free",
            "deepseek/deepseek-chat-v3-0324:free",
            "qwen/qwen-2.5-72b-instruct:free",
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
        default_model="gemini-2.0-flash",
        auth="header",
        default_headers={"x-goog-api-key": "{api_key}"},
        description="Google AI Studio free tier.",
        free_tier=True,
        models=("gemini-2.0-flash", "gemini-2.0-flash-lite", "gemini-1.5-flash"),
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
        default_model="llama3.3",
        auth="bearer",
        description="Hosted open models via Ollama Cloud (OpenAI-compatible).",
        models=("llama3.3", "qwen2.5-coder"),
    ),
    "opencode": ProviderSpec(
        key="opencode",
        label="OpenCode",
        base_url="https://opencode.ai/api/v1",
        default_model="opencode/free-coding",
        auth="bearer",
        description="Free/open coding-oriented workloads (OpenAI-compatible).",
        free_tier=True,
        models=("opencode/free-coding",),
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
    "freellmapi": ProviderSpec(
        key="freellmapi",
        label="FreeLLMAPI (self-hosted)",
        base_url="http://localhost:3001/v1",
        default_model="auto",
        auth="bearer",
        description="Self-hosted router over 34 free providers. Unified key.",
        free_tier=True,
        models=("auto",),
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


def provider_spec(provider: str) -> ProviderSpec | None:
    return PROVIDERS.get(provider)


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
        "groq": settings.groq_base_url,
        "ollama": settings.ollama_base_url,
        "openrouter": settings.openrouter_base_url,
        "together": settings.together_base_url,
        "mistral": settings.mistral_base_url,
        "gemini": settings.gemini_base_url,
        "deepseek": settings.deepseek_base_url,
        "huggingface": settings.huggingface_base_url,
        "freellmapi": settings.freellmapi_base_url,
        "tokenrouter": settings.tokenrouter_base_url,
        "nim": settings.nim_base_url,
        "ollama_cloud": settings.ollama_cloud_base_url,
        "opencode": settings.opencode_base_url,
    }
    raw = overrides.get(provider, spec.base_url) or spec.base_url
    return _sanitize_base_url(raw)


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
    ) -> None:
        self.spec = spec
        self.api_key = api_key
        self.extra_headers = extra_headers or {}
        self.timeout = timeout
        # OpenRouter attribution headers (interpolated from settings with
        # sensible defaults; never sent as literal placeholders).
        self.app_site = app_site or "https://localhost"
        self.app_name = app_name or "Bob Agent"

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.spec.auth == "bearer" and self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.spec.key == "openrouter" and self.api_key:
            headers["HTTP-Referer"] = self.app_site
            headers["X-Title"] = self.app_name
        headers.update(self.extra_headers)
        return {k: v for k, v in headers.items() if v}

    def invoke(self, model_id: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        messages = list(kwargs.pop("messages", None) or [])
        if not messages:
            messages = [{"role": "user", "content": prompt}]
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
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                f"{self.spec.base_url}/chat/completions",
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
            text = message.get("content") or ""
            finish_reason = choice.get("finish_reason")
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

    def stream(self, model_id: str, prompt: str, **kwargs: Any) -> Any:
        """Yield text deltas via SSE (``stream:true``); usage when provided.

        Returns ``(chunks, usage, tool_calls)``:
        - ``chunks``     iterator of ``str`` deltas;
        - ``usage``      dict populated from the terminal SSE chunk
          (``stream_options.include_usage``) — empty when the provider omits it,
          in which case the router flags usage as estimated, same as ``invoke``;
        - ``tool_calls`` list of canonical ``{"id", "name", "arguments"}`` calls
          assembled from the fragmented ``delta.tool_calls`` streaming chunks
          (``None`` when the provider streamed no structured calls).
        """
        messages = list(kwargs.pop("messages", None) or [])
        if not messages:
            messages = [{"role": "user", "content": prompt}]
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
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
                        f"{self.spec.base_url}/chat/completions",
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


class GeminiAdapter:
    """Google Gemini native `:generateContent` (uses `x-goog-api-key`)."""

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
        url = f"{self.spec.base_url}/models/{model_id}:generateContent"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            # x-goog-api-key header is preferred; query param as fallback.
            headers["x-goog-api-key"] = self.api_key
        headers.update(self.extra_headers)
        payload: dict[str, Any] = {"contents": [{"parts": [{"text": prompt}]}]}
        temperature = kwargs.get("temperature")
        if temperature is not None:
            payload["generationConfig"] = {"temperature": temperature}
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
            finish_reason = cand.get("finishReason")
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
            "finish_reason": str(finish_reason or "stop").lower(),
            "usage": {
                "input_tokens": int(usage_meta.get("promptTokenCount") or 0),
                "output_tokens": int(usage_meta.get("candidatesTokenCount") or 0),
                "cached_tokens": 0,
            },
            "request_id": "",
        }


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
    "freellmapi": OpenAICompatibleAdapter,
    "tokenrouter": OpenAICompatibleAdapter,
    "nim": OpenAICompatibleAdapter,
    "ollama_cloud": OpenAICompatibleAdapter,
    "opencode": OpenAICompatibleAdapter,
    "gemini": GeminiAdapter,
    "anthropic": AnthropicAdapter,
}


def build_adapter(provider: str, settings: Settings, api_key: str | None = None) -> Any | None:
    """Construct the right adapter for a provider (None if unsupported)."""
    from dataclasses import replace

    spec = provider_spec(provider)
    cls = ADAPTER_CLASSES.get(provider)
    if spec is None or cls is None:
        return None
    base_url = _sanitize_base_url(provider_base_url(provider, settings) or spec.base_url)
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


def test_provider(
    settings: Settings, provider: str, model: str | None = None, prompt: str = "ping"
) -> dict[str, Any]:
    """Reach out to a provider with a tiny request; returns diagnostic info."""
    spec = provider_spec(provider)
    if spec is None:
        return {"ok": False, "provider": provider, "error": "unknown provider"}
    api_key = settings.provider_api_key(provider)
    adapter = build_adapter(provider, settings, api_key)
    if adapter is None:
        return {"ok": False, "provider": provider, "error": "unsupported provider"}
    try:
        result = adapter.invoke(model or spec.default_model, prompt)
        return {
            "ok": True,
            "provider": provider,
            "model": model or spec.default_model,
            "output_excerpt": (result.get("output") or "")[:200],
            "usage": result.get("usage"),
        }
    except httpx.HTTPError as exc:
        return {"ok": False, "provider": provider, "error": str(exc)[:300]}
    except Exception as exc:  # noqa: BLE001 — diagnostic path
        return {
            "ok": False,
            "provider": provider,
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }


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
    router.registry.set_rule(
        SelectionRule(task_type="default", primary=router.default_model, fallback=None)
    )
    router.registry.set_rule(
        SelectionRule(task_type="*", primary=router.default_model, fallback=None)
    )
    return router
