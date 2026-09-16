"""ReAct loop — provider-agnostic Reason+Act over the capability library.

Bob's Brain/Hands split: the model reasons in text and emits tool calls; this
loop routes every call through one path (``tools/protocol.py`` to parse,
``tools/execution.py`` to validate + authorize + run) and feeds results back as
``<tool_result>`` blocks until the model answers or the iteration budget is
spent.

Two protocols are supported and produce the same internal object:

- provider-native structured tool calls, when the adapter returns them;
- Bob's fenced ```tool:name protocol, which works on every provider including
  echo/offline mode.

The loop never inspects which protocol produced a call — it only executes
:class:`ToolCall` instances.
"""

from __future__ import annotations

import json as _json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_system.services.tool_errors import (
    NeedsApprovalError,
    ToolError,
    ToolValidationError,
)
from agent_system.services.tools.execution import execute_tool
from agent_system.services.tools.protocol import (
    TOOL_FENCE_RE,
    ToolCall,
    parse_tool_calls,
    sanitize_tool_result,
    strip_tool_calls,
)
from agent_system.services.tools.registry import ToolRegistry

__all__ = [
    "LoopResult",
    "MAX_TOOL_RESULT_CHARS",
    "TOOL_FENCE_RE",
    "TRUNCATION_MARKER",
    "estimate_tokens",
    "parse_tool_calls",
    "run_tool_loop",
    "sanitize_tool_result",
    "strip_tool_calls",
]


@dataclass
class LoopResult:
    output: str
    tool_calls: int
    iterations: int
    usage: dict[str, Any] = field(default_factory=dict)
    stopped: str = "done"  # done | max_iters | error
    protocols: dict[str, int] = field(default_factory=dict)


def _merge_usage(total: dict[str, Any], part: dict[str, Any]) -> dict[str, Any]:
    for key in ("input_tokens", "output_tokens", "cached_tokens"):
        total[key] = int(total.get(key) or 0) + int(part.get(key) or 0)
    return total


def estimate_tokens(text: str) -> int:
    """Rough token estimate for context budgeting (chars/4 heuristic).

    A tokenizer-free approximation (documented, not exact): English prose
    averages ~4 chars/token across common BPE tokenizers. Used only to decide
    *when* to compact, never for billing. The authoritative implementation is
    the ContextManager (``services/context.py``), which keeps the same fallback
    and adds prioritised retention.
    """
    return max(1, len(text) // 4)


def _safe_emit(
    emit: Callable[[str, dict[str, Any]], None] | None,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Emit without ever breaking the loop (a throwing emit is a bug, not fatal)."""
    if emit is None:
        return
    try:
        emit(event_type, payload)
    except Exception:
        pass


#: Result payloads are truncated to this many JSON chars before re-entering
#: the prompt; truncation is marked so the model knows data was cut.
MAX_TOOL_RESULT_CHARS = 4000
TRUNCATION_MARKER = "\n…[truncated]"


def _render_result_block(name: str, result: dict[str, Any]) -> str:
    raw = _json.dumps(result, default=str)
    truncated = len(raw) > MAX_TOOL_RESULT_CHARS
    if truncated:
        raw = raw[:MAX_TOOL_RESULT_CHARS] + TRUNCATION_MARKER
    return f'\n\n<tool_result name="{name}">\n{sanitize_tool_result(raw)}\n</tool_result>'


def _result_block_name(block: str) -> str:
    """Extract the tool name from a rendered <tool_result> block."""
    marker = '<tool_result name="'
    start = block.find(marker)
    if start < 0:
        return "unknown"
    start += len(marker)
    end = block.find('"', start)
    return block[start:end] if end > start else "unknown"


def _resolve_budget(
    ctx: Any,
    max_context_tokens: int | None,
    compaction_threshold_pct: float | None,
) -> tuple[int, float]:
    settings = getattr(ctx, "settings", None)
    budget = (
        int(max_context_tokens)
        if max_context_tokens is not None
        else int(getattr(settings, "max_context_tokens", 100_000) or 100_000)
    )
    pct = (
        float(compaction_threshold_pct)
        if compaction_threshold_pct is not None
        else float(getattr(settings, "context_compaction_threshold_pct", 75.0) or 75.0)
    )
    return budget, pct


def _run_one_call(
    call: ToolCall,
    registry: ToolRegistry,
    ctx: Any,
    emit: Callable[[str, dict[str, Any]], None] | None,
) -> dict[str, Any]:
    """Execute one parsed call, turning every failure into a model-readable result."""
    tool = registry.get(call.name)
    if tool is None:
        _safe_emit(emit, "tool.failed", {"tool": call.name, "reason": "unknown_tool"})
        _record_tool_metric(call.name, "unknown")
        return {
            "error": "unknown_tool",
            "tool": call.name,
            "available": registry.names(),
            "hint": "Use one of the available capabilities and call again.",
        }
    if call.malformed:
        _safe_emit(emit, "tool.failed", {"tool": call.name, "reason": "malformed_arguments"})
        return {
            "error": "malformed_arguments",
            "tool": call.name,
            "detail": call.parse_error,
            "hint": "Emit the arguments as a single JSON object and call again.",
        }
    _safe_emit(
        emit,
        "tool.started",
        {"tool": tool.name, "capability_risk": tool.tier.value, "protocol": call.protocol.value},
    )
    try:
        result = execute_tool(tool, call.arguments, ctx)
    except ToolValidationError as exc:
        _safe_emit(emit, "tool.failed", {"tool": tool.name, "reason": "invalid_arguments"})
        _record_tool_metric(tool.name, tool.tier.value)
        return exc.payload()
    except NeedsApprovalError as exc:
        # agent.waiting_approval is the canonical lifecycle event for this pause.
        _safe_emit(
            emit,
            "agent.waiting_approval",
            {"tool": tool.name, "approval_id": exc.approval_id, "denied": exc.denied},
        )
        _safe_emit(
            emit,
            "tool.failed",
            {"tool": tool.name, "reason": "approval_denied" if exc.denied else "needs_approval"},
        )
        _record_tool_metric(tool.name, tool.tier.value)
        if exc.denied:
            return {
                "error": "permission_denied",
                "tool": tool.name,
                "approval_id": exc.approval_id,
                "reason": exc.reason,
                "hint": "This capability is refused by policy; choose another approach.",
            }
        return {
            "error": "needs_approval",
            "tool": tool.name,
            "approval_id": exc.approval_id,
            "hint": "The user must approve this capability, then continue without re-running it.",
        }
    except ToolError as exc:
        _safe_emit(emit, "tool.failed", {"tool": tool.name, "reason": "tool_error"})
        _record_tool_metric(tool.name, tool.tier.value)
        return {"error": "tool_error", "tool": tool.name, "detail": str(exc)}
    except Exception as exc:  # a capability crash must not kill the run
        _safe_emit(emit, "tool.failed", {"tool": tool.name, "reason": "capability_crash"})
        _record_tool_metric(tool.name, tool.tier.value)
        return {
            "error": "capability_crash",
            "tool": tool.name,
            "detail": f"{type(exc).__name__}: {exc}",
        }
    ok = "error" not in result
    _safe_emit(
        emit,
        "tool.completed" if ok else "tool.failed",
        {"tool": tool.name, "ok": ok, "capability_risk": tool.tier.value},
    )
    _record_tool_metric(tool.name, tool.tier.value)
    return result


def _record_tool_metric(name: str, risk: str) -> None:
    try:
        from agent_system.infra.telemetry import get_metrics

        get_metrics().record_tool_call(name, risk)
    except Exception:
        pass


def run_tool_loop(
    *,
    invoke: Callable[[str], dict[str, Any]],
    system: str,
    task: str,
    registry: ToolRegistry,
    ctx: Any,
    emit: Callable[[str, dict[str, Any]], None] | None = None,
    max_iters: int = 8,
    max_context_tokens: int | None = None,
    compaction_threshold_pct: float | None = None,
    compaction_keep_recent: int = 3,
) -> LoopResult:
    """Run Reason+Act until the model answers.

    ``invoke`` maps a full transcript prompt to ``{"output": str, "usage":
    {...}}`` (optionally with ``tool_calls`` for providers that return native
    structured calls). Every capability execution emits canonical
    ``tool.started`` / ``tool.completed`` / ``tool.failed`` events, and a pause
    for approval emits ``agent.waiting_approval``.

    Context budget: accumulated ``<tool_result>`` content is token-estimated,
    and when it exceeds ``compaction_threshold_pct`` of ``max_context_tokens``
    the oldest results are compacted — see ``services/context.py`` for the
    prioritised retention policy that decides what is kept.
    """
    from agent_system.services.context import ContextManager

    budget, threshold_pct = _resolve_budget(ctx, max_context_tokens, compaction_threshold_pct)
    context = ContextManager(
        max_context_tokens=budget,
        compaction_threshold_pct=threshold_pct,
        keep_recent=compaction_keep_recent,
    )
    transcript = f"{system.rstrip()}\n\n{registry.prompt_block()}\n\nTask:\n{task.strip()}"
    usage: dict[str, Any] = {}
    tool_calls = 0
    last_text = ""
    protocol_counts: dict[str, int] = {}
    for iteration in range(1, max_iters + 1):
        try:
            response = invoke(transcript)
        except Exception as exc:
            return LoopResult(
                output=last_text or f"model invocation failed: {exc}",
                tool_calls=tool_calls,
                iterations=iteration,
                usage=usage,
                stopped="error",
                protocols=protocol_counts,
            )
        text = str(response.get("output") or "")
        last_text = text
        usage = _merge_usage(usage, dict(response.get("usage") or {}))
        calls = parse_tool_calls(response)
        if not calls:
            return LoopResult(
                output=strip_tool_calls(text),
                tool_calls=tool_calls,
                iterations=iteration,
                usage=usage,
                stopped="done",
                protocols=protocol_counts,
            )
        tool_calls += len(calls)
        for call in calls:
            protocol_counts[call.protocol.value] = protocol_counts.get(call.protocol.value, 0) + 1
            result = _run_one_call(call, registry, ctx, emit)
            block = _render_result_block(call.name, result)
            transcript += block
            context.observe(block, name=call.name, result=result)
        compacted = context.compact(transcript)
        if compacted is not None:
            transcript = compacted.transcript
            _safe_emit(
                emit,
                "context.compacted",
                {
                    "dropped_count": compacted.dropped_count,
                    "kept_count": compacted.kept_count,
                    "estimated_tokens_saved": compacted.tokens_saved,
                    "retained_important": compacted.retained_important,
                },
            )
        transcript += "\n\nContinue reasoning with the results above. Answer when done."
    return LoopResult(
        output=strip_tool_calls(last_text),
        tool_calls=tool_calls,
        iterations=max_iters,
        usage=usage,
        stopped="max_iters",
        protocols=protocol_counts,
    )
