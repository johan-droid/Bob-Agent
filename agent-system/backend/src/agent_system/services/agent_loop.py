"""ReAct loop — provider-agnostic Reason+Act over the tool registry.

OpenClaw's Brain/Hands split, adapted to Bob's adapters: the model reasons
in text and emits fenced `````tool:name`` blocks; we execute them and feed
results back as ``<tool_result>`` blocks until the model answers or the
iteration budget is spent. Because the protocol lives in the prompt text
(not provider-native function calling), it works identically on OpenAI,
Anthropic, Gemini, Groq, Ollama — even echo/offline mode.
"""

from __future__ import annotations

import json as _json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_system.services.tools import NeedsApprovalError, ToolError, ToolRegistry

TOOL_FENCE_RE = re.compile(r"```tool:([A-Za-z0-9_.\-]+)\s*\n(.*?)```", re.DOTALL)
#: Matches an *opening* tool-fence so we can neutralise it in untrusted output.
_TOOL_FENCE_OPEN_RE = re.compile(r"```tool:")
#: Invisible separator inserted to break a fence so it can't be re-parsed as a
#: live  ```tool:name`` block, while staying visually identical.
_ZERO_WIDTH_SPACE = "\u200b"


def sanitize_tool_result(text: str) -> str:
    """Neutralise tool-fence substrings in untrusted tool output.

    Tool results (``web_fetch``, ``mcp_call``, …) flow back into the model
    prompt as ``<tool_result>`` blocks. If such content contained a live
    ``  ```tool:name`` fence and the model echoed or forwarded it verbatim, the
    loop would re-parse that text as a *genuine* tool call on the next
    iteration — a prompt-injection vector via untrusted tool output.

    We insert an invisible zero-width space after any opening ``  ```tool:``
    so ``TOOL_FENCE_RE`` no longer matches it, while the rendered text is
    visually identical. Only tool *results* are sanitised; the model's own
    outgoing fence parsing is untouched (legitimate calls must still work).
    """
    return _TOOL_FENCE_OPEN_RE.sub(f"```{_ZERO_WIDTH_SPACE}tool:", text)


@dataclass
class LoopResult:
    output: str
    tool_calls: int
    iterations: int
    usage: dict[str, Any] = field(default_factory=dict)
    stopped: str = "done"  # done | max_iters | error


def parse_tool_calls(text: str) -> list[tuple[str, dict[str, Any]]]:
    """Extract (name, args) from fenced tool blocks.

    Malformed JSON becomes ``{"_raw": raw}``; valid JSON that is not an
    object becomes ``{"_value": value}`` — the model always gets a dict.
    """
    calls: list[tuple[str, dict[str, Any]]] = []
    for match in TOOL_FENCE_RE.finditer(text):
        name = match.group(1).strip()
        raw = match.group(2).strip()
        try:
            args = _json.loads(raw) if raw else {}
        except _json.JSONDecodeError:
            args = {"_raw": raw}
        if not isinstance(args, dict):
            args = {"_value": args}
        calls.append((name, args))
    return calls


def strip_tool_calls(text: str) -> str:
    """Remove tool fences so the user sees the final answer, not the protocol."""
    return TOOL_FENCE_RE.sub("", text).strip()


def _merge_usage(total: dict[str, Any], part: dict[str, Any]) -> dict[str, Any]:
    for key in ("input_tokens", "output_tokens", "cached_tokens"):
        total[key] = int(total.get(key) or 0) + int(part.get(key) or 0)
    return total


def estimate_tokens(text: str) -> int:
    """Rough token estimate for context budgeting (chars/4 heuristic).

    This is a tokenizer-free approximation (documented, not exact): English
    prose averages ~4 chars/token across common BPE tokenizers. It is only
    used to decide *when* to compact tool results, never for billing.
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
    raw = _json.dumps(result)
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

    ``invoke`` maps a full transcript prompt to ``{"output": str,
    "usage": {...}}`` (e.g. a ``ModelRouter.invoke`` partial). Every tool
    execution fires ``tool.called`` / ``tool.result`` through ``emit`` when
    given. Unknown tools, approval blocks, and crashes become error results
    the model can react to — never silent.

    Context budget: accumulated ``<tool_result>`` content is token-estimated
    (see ``estimate_tokens``). When it exceeds ``compaction_threshold_pct`` of
    ``max_context_tokens`` (explicit args win, else ``ctx.settings``), the
    oldest results are dropped in favour of a one-line synthetic summary and
    a ``context.compacted`` event fires — the loop degrades gracefully
    instead of blowing the model context window.
    """
    budget, threshold_pct = _resolve_budget(ctx, max_context_tokens, compaction_threshold_pct)
    threshold = budget * (threshold_pct / 100.0)
    transcript = f"{system.rstrip()}\n\n{registry.prompt_block()}\n\nTask:\n{task.strip()}"
    usage: dict[str, Any] = {}
    tool_calls = 0
    last_text = ""
    result_blocks: list[str] = []
    result_tokens = 0
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
            )
        text = str(response.get("output") or "")
        last_text = text
        usage = _merge_usage(usage, dict(response.get("usage") or {}))
        calls = parse_tool_calls(text)
        if not calls:
            return LoopResult(
                output=strip_tool_calls(text),
                tool_calls=tool_calls,
                iterations=iteration,
                usage=usage,
                stopped="done",
            )
        tool_calls += len(calls)
        for name, args in calls:
            tool = registry.get(name)
            if tool is None:
                _safe_emit(emit, "tool.called", {"tool": name, "risk": "unknown"})
                result = {
                    "error": f"unknown tool '{name}'. Available: {', '.join(registry.names())}"
                }
                _safe_emit(emit, "tool.result", {"tool": name, "ok": False})
                try:
                    from agent_system.infra.telemetry import get_metrics

                    get_metrics().record_tool_call(name, "unknown")
                except Exception:
                    pass
            else:
                _safe_emit(emit, "tool.called", {"tool": name, "risk": tool.risk})
                try:
                    result = tool.handler(args, ctx)
                except NeedsApprovalError as exc:
                    result = {
                        "error": str(exc),
                        "approval_id": exc.approval_id,
                        "hint": (
                            "Ask the user to approve, then continue without re-running the command."
                        ),
                    }
                except ToolError as exc:
                    result = {"error": str(exc)}
                except Exception as exc:
                    result = {"error": f"{type(exc).__name__}: {exc}"}
                _safe_emit(
                    emit,
                    "tool.result",
                    {"tool": name, "ok": "error" not in result},
                )
                try:
                    from agent_system.infra.telemetry import get_metrics

                    get_metrics().record_tool_call(name, tool.risk)
                except Exception:
                    pass
            block = _render_result_block(name, result)
            transcript += block
            result_blocks.append(block)
            result_tokens += estimate_tokens(block)
        if result_tokens > threshold and len(result_blocks) > 1:
            keep = max(1, min(compaction_keep_recent, len(result_blocks) - 1))
            # When only a few blocks exist but one is gigantic, still
            # compact down to the single newest block instead of never
            # compacting (previous guard required len > keep_recent).
            dropped = len(result_blocks) - keep
            dropped_tokens = sum(estimate_tokens(block) for block in result_blocks[:dropped])
            dropped_names = [_result_block_name(block) for block in result_blocks[:dropped]]
            for block in result_blocks[:dropped]:
                transcript = transcript.replace(block, "", 1)
            summary = (
                f'\n\n<tool_result name="context-summary">\n'
                f"Earlier tool results compacted: {dropped} result(s) "
                f"({', '.join(dropped_names)}) dropped to stay within "
                f"context budget; newest {keep} kept."
                f"\n</tool_result>"
            )
            transcript += summary
            result_blocks = result_blocks[dropped:]
            # Account for the summary itself so the budget does not
            # systematically undercount after every compaction.
            result_tokens = result_tokens - dropped_tokens + estimate_tokens(summary)
            _safe_emit(
                emit,
                "context.compacted",
                {
                    "dropped_count": dropped,
                    "estimated_tokens_saved": dropped_tokens,
                },
            )
        transcript += "\n\nContinue reasoning with the results above. Answer when done."
    return LoopResult(
        output=strip_tool_calls(last_text),
        tool_calls=tool_calls,
        iterations=max_iters,
        usage=usage,
        stopped="max_iters",
    )


__all__ = [
    "LoopResult",
    "MAX_TOOL_RESULT_CHARS",
    "TRUNCATION_MARKER",
    "estimate_tokens",
    "parse_tool_calls",
    "run_tool_loop",
    "sanitize_tool_result",
    "strip_tool_calls",
]
