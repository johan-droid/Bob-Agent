"""Normalized LLM contract — provider-agnostic core (OpenAI-style).

This module is the single seam between Bob's agent runtime and every
provider adapter in ``services/providers.py``:

- :class:`NormalizedLLMRequest` — one internal request shape. Adapters
  transform it into native wire payloads via pure functions; the original
  request is never mutated.
- :class:`NormalizedLLMResponse` — one internal response shape
  (content / tool_calls / finish_reason / usage / model / provider /
  request_id / metadata). Arguments are JSON-validated before execution.
- :class:`StreamEvent` — provider-independent streaming events
  (TEXT_DELTA / TOOL_CALL_START / TOOL_CALL_DELTA / TOOL_CALL_END /
  USAGE / DONE / ERROR). Adapters translate native SSE into these events;
  Telegram only ever sees finalized user-facing content.
- :class:`ProviderError` — structured error taxonomy with retryability,
  cooldown hints and user-safe messages. Never log keys.
- :class:`ProviderAdapter` — conceptual interface every adapter implements
  (validateCredentials / resolveModel / buildRequest / send / parseResponse
  / parseStream / normalizeToolCalls / classifyError / getCapabilities).

Additive by design: existing ``invoke``/``stream`` dict-based adapters keep
working. New code should prefer these dataclasses; helpers below convert
between the two shapes.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Normalized request
# ---------------------------------------------------------------------------


@dataclass
class NormalizedLLMRequest:
    """One internal request shape for every provider."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    system: str | None = None
    model: str = ""
    temperature: float | None = None
    max_output_tokens: int | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = None
    response_format: dict[str, Any] | None = None
    stream: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def correlation_id(self) -> str:
        return str(
            self.metadata.get("request_id")
            or self.metadata.get("correlation_id")
            or f"req_{uuid.uuid4().hex[:12]}"
        )


def build_normalized_request(
    prompt: str = "",
    *,
    messages: list[dict[str, Any]] | None = None,
    model: str = "",
    system: str | None = None,
    temperature: float | None = None,
    max_output_tokens: int | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any | None = None,
    response_format: dict[str, Any] | None = None,
    stream: bool = False,
    metadata: dict[str, Any] | None = None,
    **kwargs: Any,
) -> NormalizedLLMRequest:
    """Build a normalized request without mutating caller inputs."""
    msgs = [dict(m) for m in (messages or [])] if messages else []
    if not msgs and prompt:
        msgs = [{"role": "user", "content": prompt}]
    meta = dict(metadata or {})
    # Preserve correlation identifiers when callers pass them as kwargs.
    for key in (
        "request_id",
        "correlation_id",
        "conversation_id",
        "message_id",
        "task_id",
        "session_id",
        "provider",
        "model",
        "attempt",
        "tool_iteration",
    ):
        if key in kwargs and key not in meta:
            meta[key] = kwargs[key]
    return NormalizedLLMRequest(
        messages=msgs,
        system=system,
        model=model or str(kwargs.get("model_id") or ""),
        temperature=temperature if temperature is not None else kwargs.get("temperature"),
        max_output_tokens=max_output_tokens
        if max_output_tokens is not None
        else (kwargs.get("max_output_tokens") or kwargs.get("max_tokens")),
        tools=[dict(t) for t in tools] if tools is not None else kwargs.get("tools"),
        tool_choice=tool_choice if tool_choice is not None else kwargs.get("tool_choice"),
        response_format=response_format
        if response_format is not None
        else kwargs.get("response_format"),
        stream=bool(stream or kwargs.get("stream", False)),
        metadata=meta,
    )


# ---------------------------------------------------------------------------
# Normalized tool calls + response
# ---------------------------------------------------------------------------


@dataclass
class NormalizedToolCall:
    id: str
    name: str
    arguments: str  # JSON-encoded object
    metadata: dict[str, Any] = field(default_factory=dict)

    def arguments_dict(self) -> tuple[dict[str, Any], str | None]:
        return validate_tool_arguments(self.arguments)


def validate_tool_arguments(raw: Any) -> tuple[dict[str, Any], str | None]:
    """Validate tool arguments as JSON object. Returns (dict, parse_error)."""
    if raw is None or raw == "":
        return {}, None
    if isinstance(raw, dict):
        return raw, None
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            return {"_raw": raw}, f"arguments are not valid JSON: {exc.msg}"
        if isinstance(value, dict):
            return value, None
        return {"_value": value}, "arguments must be a JSON object"
    return {"_value": raw}, "arguments must be a JSON object"


_FENCED_TOOL_RE = re.compile(r"```tool:([A-Za-z0-9_.\-]+)\s*\n(.*?)```", re.DOTALL)


def rescue_tool_calls_from_text(text: str) -> list[NormalizedToolCall]:
    """Controlled rescue for providers emitting plain-text tool calls.

    Only Bob's explicit fenced `````tool:name`` protocol is rescued — never
    arbitrary text. Malformed JSON is preserved with a parse error surfaced
    via :func:`validate_tool_arguments` downstream; nothing here executes.
    """
    calls: list[NormalizedToolCall] = []
    for index, match in enumerate(_FENCED_TOOL_RE.finditer(text or "")):
        name = match.group(1).strip()
        raw = match.group(2).strip() or "{}"
        if not name:
            continue
        calls.append(
            NormalizedToolCall(
                id=f"call_rescued_{index + 1}",
                name=name,
                arguments=raw if isinstance(raw, str) else json.dumps(raw),
                metadata={"rescued": True, "protocol": "bob_fenced"},
            )
        )
    return calls


def normalize_tool_calls(raw_items: Any) -> list[NormalizedToolCall]:
    """Normalize OpenAI/Gemini/Anthropic native shapes into one carrier."""
    out: list[NormalizedToolCall] = []
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
        out.append(
            NormalizedToolCall(
                id=str(item.get("id") or f"call_native_{index + 1}"),
                name=str(name),
                arguments=raw,
            )
        )
    return out


@dataclass
class NormalizedLLMResponse:
    content: str = ""
    tool_calls: list[NormalizedToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    provider: str = ""
    request_id: str = ""
    raw_error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_adapter_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "content": self.content,
            "output": self.content,
            "tool_calls": [
                {"id": c.id, "name": c.name, "arguments": c.arguments} for c in self.tool_calls
            ],
            "finish_reason": self.finish_reason,
            "usage": dict(self.usage),
            "request_id": self.request_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_adapter_dict(cls, data: dict[str, Any]) -> NormalizedLLMResponse:
        calls = normalize_tool_calls(data.get("tool_calls"))
        usage = dict(data.get("usage") or {})
        return cls(
            content=str(data.get("content") or data.get("output") or ""),
            tool_calls=calls,
            finish_reason=str(data.get("finish_reason") or "stop"),
            usage=usage,
            model=str(data.get("model") or ""),
            provider=str(data.get("provider") or ""),
            request_id=str(data.get("request_id") or ""),
            metadata=dict(data.get("metadata") or {}),
        )


# ---------------------------------------------------------------------------
# Streaming events
# ---------------------------------------------------------------------------


class StreamEventType(StrEnum):
    TEXT_DELTA = "TEXT_DELTA"
    TOOL_CALL_START = "TOOL_CALL_START"
    TOOL_CALL_DELTA = "TOOL_CALL_DELTA"
    TOOL_CALL_END = "TOOL_CALL_END"
    USAGE = "USAGE"
    DONE = "DONE"
    ERROR = "ERROR"


@dataclass
class StreamEvent:
    type: StreamEventType
    data: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


def text_deltas_to_events(chunks: Any) -> Any:
    """Adapt a legacy str-chunk generator into StreamEvents (additive)."""
    for delta in chunks:
        yield StreamEvent(type=StreamEventType.TEXT_DELTA, data=str(delta))
    yield StreamEvent(type=StreamEventType.DONE)


# ---------------------------------------------------------------------------
# Structured errors
# ---------------------------------------------------------------------------


class ProviderErrorCode(StrEnum):
    AUTHENTICATION = "AUTHENTICATION"
    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_MODEL = "INVALID_MODEL"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    TIMEOUT = "TIMEOUT"
    NETWORK = "NETWORK"
    SERVER_ERROR = "SERVER_ERROR"
    TOOL_UNSUPPORTED = "TOOL_UNSUPPORTED"
    STRUCTURED_OUTPUT_UNSUPPORTED = "STRUCTURED_OUTPUT_UNSUPPORTED"
    CONTENT_POLICY = "CONTENT_POLICY"
    UNKNOWN = "UNKNOWN"


@dataclass
class ProviderError:
    provider: str = ""
    model: str = ""
    http_status: int | None = None
    provider_code: str = ""
    code: ProviderErrorCode = ProviderErrorCode.UNKNOWN
    message: str = ""
    retryable: bool = False
    cooldown_seconds: float = 0.0
    user_safe_message: str = ""

    def to_string(self) -> str:
        status = f" {self.http_status}" if self.http_status else ""
        return f"{type(self).__name__}{status} [{self.code.value}]: {self.message[:300]}"


def _safe_message_for(code: ProviderErrorCode) -> str:
    return {
        ProviderErrorCode.AUTHENTICATION: "The provider rejected the API key. Check configuration.",
        ProviderErrorCode.INVALID_REQUEST: "The request was rejected by the provider.",
        ProviderErrorCode.INVALID_MODEL: "The configured model is not available on this provider.",
        ProviderErrorCode.MODEL_UNAVAILABLE: "The model is temporarily unavailable.",
        ProviderErrorCode.RATE_LIMITED: "Rate limited — trying another provider.",
        ProviderErrorCode.QUOTA_EXHAUSTED: ("Provider quota exhausted — trying another provider."),
        ProviderErrorCode.TIMEOUT: ("The provider timed out — retrying elsewhere."),
        ProviderErrorCode.NETWORK: "Network error reaching the provider.",
        ProviderErrorCode.SERVER_ERROR: (
            "The provider had an internal error — retrying elsewhere."
        ),
        ProviderErrorCode.TOOL_UNSUPPORTED: ("The model does not support tool calling."),
        ProviderErrorCode.STRUCTURED_OUTPUT_UNSUPPORTED: (
            "Structured output is not supported by this model."
        ),
        ProviderErrorCode.CONTENT_POLICY: "The provider refused the content.",
        ProviderErrorCode.UNKNOWN: "The provider request failed.",
    }[code]


def classify_provider_error(
    exc_or_msg: Any,
    *,
    provider: str = "",
    model: str = "",
    status_code: int | None = None,
) -> ProviderError:
    """Classify any provider failure into a structured ProviderError."""
    code = status_code
    response = getattr(exc_or_msg, "response", None)
    if code is None and response is not None and hasattr(response, "status_code"):
        try:
            code = int(response.status_code)
        except (TypeError, ValueError):
            code = None
    # httpx timeout / connect errors carry no status.
    msg = str(exc_or_msg or "")
    response_text = ""
    try:
        response_text = str(getattr(response, "text", "") or "")
    except Exception:
        response_text = ""
    low = f"{msg}\n{response_text}".lower()
    provider_code = ""
    try:
        body = getattr(response, "text", "") if response is not None else ""
        if body:
            parsed = json.loads(body) if isinstance(body, str) else {}
            if isinstance(parsed, dict):
                err = parsed.get("error")
                if isinstance(err, dict):
                    provider_code = str(err.get("code") or err.get("type") or "")
                elif isinstance(err, str):
                    provider_code = err[:80]
    except (ValueError, TypeError, AttributeError):
        pass

    def build(
        error_code: ProviderErrorCode, retryable: bool, cooldown: float = 0.0
    ) -> ProviderError:
        return ProviderError(
            provider=provider,
            model=model,
            http_status=code,
            provider_code=provider_code,
            code=error_code,
            message=msg[:500],
            retryable=retryable,
            cooldown_seconds=cooldown,
            user_safe_message=_safe_message_for(error_code),
        )

    if (
        code in (401, 403)
        or "401" in low
        or "403" in low
        or "invalid api key" in low
        or "unauthorized" in low
        or "authentication" in low
    ):
        return build(ProviderErrorCode.AUTHENTICATION, False)
    if (
        code == 404
        or "404" in low
        or "unknown model" in low
        or "does not exist" in low
        or "model_not_found" in low
        or "not found" in low
    ):
        # Distinguish invalid model ID from missing endpoint when possible.
        if "model" in low:
            return build(ProviderErrorCode.INVALID_MODEL, False)
        return build(ProviderErrorCode.INVALID_MODEL, False)
    if "quota" in low or "exhausted" in low or "insufficient_quota" in low or "billing" in low:
        return build(ProviderErrorCode.QUOTA_EXHAUSTED, True, 60.0)
    if (
        code == 429
        or "429" in low
        or "rate_limit" in low
        or "rate limit" in low
        or "retry-after" in low
        or "too many requests" in low
        or "too many" in low
    ):
        return build(ProviderErrorCode.RATE_LIMITED, True, 60.0)
    if "tool" in low and ("support" in low or "unsupported" in low):
        return build(ProviderErrorCode.TOOL_UNSUPPORTED, False)
    if (
        "response_format" in low
        or "structured" in low
        or "json_schema" in low
        or "json mode" in low
    ):
        return build(ProviderErrorCode.STRUCTURED_OUTPUT_UNSUPPORTED, False)
    if code == 400 or code == 422 or "invalid_request" in low or "bad request" in low:
        return build(ProviderErrorCode.INVALID_REQUEST, False)
    if (
        "safety" in low
        or "content_filter" in low
        or "blocked" in low
        or "policy" in low
        or "prohibited" in low
    ):
        return build(ProviderErrorCode.CONTENT_POLICY, False)
    if "timeout" in low or "timed out" in low:
        return build(ProviderErrorCode.TIMEOUT, True, 10.0)
    if (
        "connecterror" in low
        or "connection" in low
        or "network" in low
        or "dns" in low
        or "unreachable" in low
    ):
        return build(ProviderErrorCode.NETWORK, True, 10.0)
    if code is not None and code >= 500:
        return build(ProviderErrorCode.SERVER_ERROR, True, 15.0)
    if code in (408, 425, 502, 503, 504):
        return build(ProviderErrorCode.SERVER_ERROR, True, 15.0)
    for marker in ("500", "502", "503", "504", "server error", "internal error"):
        if marker in low:
            return build(ProviderErrorCode.SERVER_ERROR, True, 15.0)
    return build(ProviderErrorCode.UNKNOWN, True, 5.0)


# ---------------------------------------------------------------------------
# Adapter interface
# ---------------------------------------------------------------------------


@dataclass
class ProviderCapabilities:
    provider: str = ""
    supports_tools: bool = False
    supports_streaming: bool = False
    supports_structured_output: bool = False
    supports_vision: bool = False
    supports_parallel_tools: bool = False
    api_surface: str = "chat"


class ProviderAdapterBase:
    """Conceptual interface every provider adapter implements.

    Existing adapters satisfy this structurally via ``invoke``/``stream``;
    new adapters should override the granular hooks. All transforms are
    pure where possible — never mutate the incoming normalized request.
    """

    provider_id: str = ""
    supports_streaming: bool = False

    def validateCredentials(self, api_key: str | None) -> tuple[bool, str]:
        raise NotImplementedError

    def resolveModel(self, model_id: str) -> str:
        return (model_id or "").strip()

    def buildRequest(self, request: NormalizedLLMRequest) -> dict[str, Any]:
        raise NotImplementedError

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def parseResponse(self, data: dict[str, Any]) -> NormalizedLLMResponse:
        return NormalizedLLMResponse.from_adapter_dict(data)

    def parseStream(self, chunks: Any) -> Any:
        return text_deltas_to_events(chunks)

    def normalizeToolCalls(self, raw: Any) -> list[NormalizedToolCall]:
        return normalize_tool_calls(raw)

    def classifyError(self, exc: Any, **ctx: Any) -> ProviderError:
        return classify_provider_error(exc, **ctx)

    def getCapabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(provider=self.provider_id)


# ---------------------------------------------------------------------------
# Observability (secret-safe)
# ---------------------------------------------------------------------------


_CORRELATION_KEYS = (
    "request_id",
    "correlation_id",
    "conversation_id",
    "message_id",
    "task_id",
    "session_id",
    "provider",
    "model",
    "attempt",
    "tool_iteration",
)


def correlation_context(metadata: dict[str, Any] | None) -> str:
    meta = metadata or {}
    parts = [f"{k}={meta[k]}" for k in _CORRELATION_KEYS if meta.get(k) not in (None, "")]
    return " ".join(parts)


def llm_log(event: str, metadata: dict[str, Any] | None = None, **fields: Any) -> None:
    """Structured [LLM] log line. Never include keys or Authorization headers."""
    safe = {
        k: v
        for k, v in (fields or {}).items()
        if "key" not in k.lower() and "authoriz" not in k.lower()
    }
    ctx = correlation_context(metadata)
    suffix = f" {ctx}" if ctx else ""
    detail = " ".join(f"{k}={v}" for k, v in safe.items())
    logger.info("[LLM] %s%s%s", event, f" {detail}" if detail else "", suffix)


def redact_secrets(text: str, secrets: list[str | None]) -> str:
    out = text or ""
    for secret in secrets:
        if secret and len(secret) >= 4 and secret in out:
            out = out.replace(secret, "***")
    # Bearer tokens that slipped through formatting.
    out = re.sub(r"Bearer\s+[A-Za-z0-9._\-]{8,}", "Bearer ***", out)
    return out


__all__ = [
    "NormalizedLLMRequest",
    "NormalizedLLMResponse",
    "NormalizedToolCall",
    "ProviderAdapterBase",
    "ProviderCapabilities",
    "ProviderError",
    "ProviderErrorCode",
    "StreamEvent",
    "StreamEventType",
    "build_normalized_request",
    "classify_provider_error",
    "correlation_context",
    "llm_log",
    "normalize_tool_calls",
    "redact_secrets",
    "rescue_tool_calls_from_text",
    "text_deltas_to_events",
    "validate_tool_arguments",
]
