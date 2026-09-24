"""Tool-call protocols — one internal object, many wire formats (v3.1 §17).

The model's tool call may arrive in two shapes:

1. **Provider-native structured tool calls** — OpenAI/Anthropic/Gemini style
   function-call objects returned alongside the message.
2. **Bob's fenced protocol** — a ```tool:name block in the model's text, which
   is what makes Bob provider-agnostic and works on every adapter including
   echo/offline mode.

Both are parsed into the same :class:`ToolCall`, so nothing downstream knows or
cares which protocol produced it. Execution depends only on the internal object.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

TOOL_FENCE_RE = re.compile(r"```tool:([A-Za-z0-9_.\-]+)\s*\n(.*?)```", re.DOTALL)
#: Matches an *opening* tool-fence so we can neutralise it in untrusted output.
_TOOL_FENCE_OPEN_RE = re.compile(r"```tool:")
#: Invisible separator inserted to break a fence so it can't be re-parsed as a
#: live  ```tool:name`` block, while staying visually identical.
_ZERO_WIDTH_SPACE = "\u200b"


# Third wire format: the ``<invoke name="x">…</invoke>`` XML shape that some
# models emit when they were trained on plugin/function syntax rather than
# JSON tool_calls or Bob's fence. Without this parser those calls are silently
# dropped, the loop ends on iteration 1, and no tool ever runs.
_INVOKE_BLOCK_RE = re.compile(
    r"<(?:[A-Za-z0-9_]+:)?invoke\s+name=[\"']([^\"']+)[\"'][^>]*>"
    r"(.*?)"
    r"</(?:[A-Za-z0-9_]+:)?invoke>",
    re.DOTALL | re.IGNORECASE,
)
_INVOKE_PARAM_RE = re.compile(
    r"<(?:[A-Za-z0-9_]+:)?parameter\s+name=[\"']([^\"']+)[\"'][^>]*>"
    r"(.*?)"
    r"</(?:[A-Za-z0-9_]+:)?parameter>",
    re.DOTALL | re.IGNORECASE,
)
#: Opening tag shape, used to neutralise injected invoke blocks in tool output.
_INVOKE_OPEN_RE = re.compile(r"<((?:[A-Za-z0-9_]+:)?invoke\s+name=)", re.IGNORECASE)


class ProtocolKind(StrEnum):
    """Which protocol produced a call."""

    FENCED = "bob_fenced"
    NATIVE = "provider_native"
    XML_INVOKE = "xml_invoke"


@dataclass(frozen=True)
class ToolCall:
    """The one internal representation every protocol must produce."""

    id: str
    name: str
    arguments: dict[str, Any]
    source: str
    protocol: ProtocolKind
    raw_arguments: str | None = None
    parse_error: str | None = None

    @property
    def malformed(self) -> bool:
        return self.parse_error is not None


class ToolCallProtocol:
    """Base class: a protocol turns a model message into :class:`ToolCall`s."""

    kind: ProtocolKind = ProtocolKind.FENCED

    def supports(self, message: Mapping[str, Any]) -> bool:
        """True when this protocol can extract calls from the message."""
        raise NotImplementedError

    def parse(self, message: Mapping[str, Any]) -> list[ToolCall]:
        raise NotImplementedError


def _coerce_arguments(raw: Any) -> tuple[dict[str, Any], str | None]:
    """Turn whatever the model produced into (dict, parse_error)."""
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


class BobFencedProtocol(ToolCallProtocol):
    """Bob's textual fenced protocol — the provider-agnostic fallback."""

    kind = ProtocolKind.FENCED

    def supports(self, message: Mapping[str, Any]) -> bool:
        return "```tool:" in str(message.get("output") or "")

    def parse(self, message: Mapping[str, Any]) -> list[ToolCall]:
        text = str(message.get("output") or "")
        calls: list[ToolCall] = []
        for index, match in enumerate(TOOL_FENCE_RE.finditer(text)):
            name = match.group(1).strip()
            raw = match.group(2).strip()
            arguments, error = _coerce_arguments(raw if raw else None)
            calls.append(
                ToolCall(
                    id=f"call_fenced_{index + 1}",
                    name=name,
                    arguments=arguments,
                    source="assistant_text",
                    protocol=self.kind,
                    raw_arguments=raw,
                    parse_error=error,
                )
            )
        return calls


class NativeToolCallProtocol(ToolCallProtocol):
    """Provider-native structured tool calls (``message['tool_calls']``).

    Accepts both the OpenAI shape (``{id, function: {name, arguments}}``) and
    the flat shape used by other adapters (``{id, name, arguments}``).
    """

    kind = ProtocolKind.NATIVE

    def __init__(self, key: str = "tool_calls") -> None:
        self._key = key

    def supports(self, message: Mapping[str, Any]) -> bool:
        return bool(message.get(self._key))

    def parse(self, message: Mapping[str, Any]) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for index, item in enumerate(self._as_items(message.get(self._key))):
            if not isinstance(item, Mapping):
                continue
            function = item.get("function")
            if isinstance(function, Mapping):
                name = str(function.get("name") or "")
                raw: Any = function.get("arguments")
            else:
                name = str(item.get("name") or "")
                raw = item.get("arguments")
            if not name:
                continue
            arguments, error = _coerce_arguments(raw)
            calls.append(
                ToolCall(
                    id=str(item.get("id") or f"call_native_{index + 1}"),
                    name=name,
                    arguments=arguments,
                    source="provider_tool_call",
                    protocol=self.kind,
                    raw_arguments=raw if isinstance(raw, str) else None,
                    parse_error=error,
                )
            )
        return calls

    @staticmethod
    def _as_items(value: Any) -> Sequence[Any]:
        if isinstance(value, (list, tuple)):
            return value
        return []


class XmlInvokeProtocol(ToolCallProtocol):
    """``<invoke name="x">...</invoke>`` XML tool calls (plugin syntax).

    Two body shapes are accepted:

    - a raw JSON object (the shape observed in the wild), coerced by the shared
      ``_coerce_arguments`` so malformed JSON is *flagged*, never dropped;
    - name/value ``parameter`` elements assembled into an object (a value that
      parses as JSON keeps its type, otherwise it stays a string).

    Registered last so native structured calls and Bob's fence keep winning
    when a provider honours either of them.
    """

    kind = ProtocolKind.XML_INVOKE

    def supports(self, message: Mapping[str, Any]) -> bool:
        return bool(_INVOKE_BLOCK_RE.search(str(message.get("output") or "")))

    def parse(self, message: Mapping[str, Any]) -> list[ToolCall]:
        text = str(message.get("output") or "")
        calls: list[ToolCall] = []
        for index, match in enumerate(_INVOKE_BLOCK_RE.finditer(text)):
            name = match.group(1).strip()
            body = (match.group(2) or "").strip()
            if not name:
                continue
            params = _INVOKE_PARAM_RE.findall(body)
            if params:
                arguments: dict[str, Any] = {}
                error: str | None = None
                for key, raw_value in params:
                    value = raw_value.strip()
                    try:
                        arguments[key] = json.loads(value)
                    except ValueError:
                        arguments[key] = value
            else:
                arguments, error = _coerce_arguments(body or None)
            calls.append(
                ToolCall(
                    id=f"call_invoke_{index + 1}",
                    name=name,
                    arguments=arguments,
                    source="assistant_text",
                    protocol=self.kind,
                    raw_arguments=body or None,
                    parse_error=error,
                )
            )
        return calls


#: Ordered protocols: provider-native first (more reliable when present), then
#: Bob's fenced protocol, then the XML invoke shape as the last textual
#: fallback for models that ignore both earlier instructions.
DEFAULT_PROTOCOLS: tuple[ToolCallProtocol, ...] = (
    NativeToolCallProtocol(),
    BobFencedProtocol(),
    XmlInvokeProtocol(),
)


def parse_tool_calls(
    message: Mapping[str, Any] | str,
    protocols: Iterable[ToolCallProtocol] = DEFAULT_PROTOCOLS,
) -> list[ToolCall]:
    """Extract every :class:`ToolCall` from a model message.

    Accepts either the model message mapping (so provider-native structured
    calls are honoured) or a bare string of model text, which is treated as
    ``{"output": text}`` for callers that only have the text.

    Ordering: native structured calls win when the provider returned them;
    the fenced protocol is consulted as a fallback so mixed/legacy adapters
    keep working.
    """
    payload: Mapping[str, Any] = {"output": message} if isinstance(message, str) else message
    for protocol in protocols:
        if protocol.supports(payload):
            return protocol.parse(payload)
    return []


def tool_calls_as_pairs(calls: Sequence[ToolCall]) -> list[tuple[str, dict[str, Any]]]:
    """Legacy ``(name, args)`` view (kept for existing call sites/tests)."""
    return [(call.name, call.arguments) for call in calls]


def sanitize_tool_result(text: str) -> str:
    """Neutralise tool-fence substrings in untrusted tool output.

    Tool results (web fetch, MCP call, …) flow back into the model prompt as
    ``<tool_result>`` blocks. If such content contained a live ```tool:name
    fence and the model echoed or forwarded it verbatim, the loop would
    re-parse that text as a *genuine* tool call on the next iteration — a
    prompt-injection vector via untrusted tool output.

    We insert an invisible zero-width space after any opening ```tool: so the
    fence regex no longer matches it, while the rendered text is visually
    identical. The same break is applied to an opening ``<invoke name=`` tag,
    because that third wire format is now a live protocol too — leaving it
    neutralisable would reopen the identical injection vector through the new
    parser. Only tool *results* are sanitised; the model's own outgoing calls
    are untouched (legitimate calls must still work).
    """
    broken_fence = _TOOL_FENCE_OPEN_RE.sub(f"```{_ZERO_WIDTH_SPACE}tool:", text)
    return _INVOKE_OPEN_RE.sub(f"<{_ZERO_WIDTH_SPACE}\\1", broken_fence)


def strip_tool_calls(text: str) -> str:
    """Remove tool-call protocol so the user sees the final answer, not the protocol."""
    without_invoke = _INVOKE_BLOCK_RE.sub("", text)
    return TOOL_FENCE_RE.sub("", without_invoke).strip()


@dataclass
class ProtocolStats:
    """Which protocols produced calls (observability for provider drift)."""

    counts: dict[str, int] = field(default_factory=dict)

    def record(self, calls: Sequence[ToolCall]) -> None:
        for call in calls:
            key = call.protocol.value
            self.counts[key] = self.counts.get(key, 0) + 1


__all__ = [
    "BobFencedProtocol",
    "DEFAULT_PROTOCOLS",
    "NativeToolCallProtocol",
    "ProtocolKind",
    "ProtocolStats",
    "TOOL_FENCE_RE",
    "ToolCall",
    "ToolCallProtocol",
    "XmlInvokeProtocol",
    "parse_tool_calls",
    "sanitize_tool_result",
    "strip_tool_calls",
    "tool_calls_as_pairs",
]


#: Type alias kept for call sites that pass a custom parser.
Parser = Callable[[Mapping[str, Any]], list[ToolCall]]
