"""P1 — Context/token budget compaction for the ReAct loop.

Acceptance: an oversized tool-result chain triggers graceful compaction
(oldest results dropped + one-line summary + ``context.compacted`` event),
not failure; normal short loops never compact.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from agent_system.services.agent_loop import estimate_tokens, run_tool_loop
from agent_system.services.tools import Tool, ToolRegistry


def _registry(payload: str) -> ToolRegistry:
    def big(_args: dict[str, Any], _ctx: Any) -> dict[str, Any]:
        return {"content": payload}

    reg = ToolRegistry()
    reg.register(Tool("big", "returns a large payload", {"properties": {}}, "read", big))
    return reg


class TestEstimateTokens:
    def test_chars_over_four_heuristic(self) -> None:
        assert estimate_tokens("abcd" * 100) == 100
        assert estimate_tokens("") == 1  # never zero: empty still costs

    def test_documented_approximation(self) -> None:
        assert estimate_tokens("x" * 8) == 2


class TestCompaction:
    def test_oversized_results_trigger_compaction_not_failure(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []
        payload = "x" * 4000  # ~1000 estimated tokens per result
        registry = _registry(payload)
        calls = {"n": 0}

        def invoke(_transcript: str) -> dict[str, Any]:
            calls["n"] += 1
            if calls["n"] <= 4:
                return {"output": "```tool:big\n{}\n```"}
            return {"output": "done: summarized the large results"}

        result = run_tool_loop(
            invoke=invoke,
            system="sys",
            task="read the big payloads",
            registry=registry,
            ctx=SimpleNamespace(settings=SimpleNamespace()),
            emit=lambda t, p: events.append((t, p)),
            max_iters=6,
            max_context_tokens=1000,
            compaction_threshold_pct=75.0,
            compaction_keep_recent=1,
        )
        assert result.stopped == "done"
        compacted = [p for t, p in events if t == "context.compacted"]
        assert compacted, "expected a context.compacted event"
        assert compacted[0]["dropped_count"] >= 1
        assert compacted[0]["estimated_tokens_saved"] > 0

    def test_normal_short_loops_never_compact(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []
        registry = _registry("small")
        seen = {"n": 0}

        def invoke(_transcript: str) -> dict[str, Any]:
            seen["n"] += 1
            if seen["n"] == 1:
                return {"output": "```tool:big\n{}\n```"}
            return {"output": "done"}

        result = run_tool_loop(
            invoke=invoke,
            system="sys",
            task="quick read",
            registry=registry,
            ctx=SimpleNamespace(settings=SimpleNamespace()),
            emit=lambda t, p: events.append((t, p)),
            max_iters=4,
        )
        assert result.stopped == "done"
        assert [t for t, _ in events if t == "context.compacted"] == []

    def test_budget_resolves_from_settings(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []
        registry = _registry("y" * 4000)
        calls = {"n": 0}

        def invoke(_transcript: str) -> dict[str, Any]:
            calls["n"] += 1
            if calls["n"] <= 4:
                return {"output": "```tool:big\n{}\n```"}
            return {"output": "done"}

        settings = SimpleNamespace(max_context_tokens=1000, context_compaction_threshold_pct=75.0)
        run_tool_loop(
            invoke=invoke,
            system="sys",
            task="t",
            registry=registry,
            ctx=SimpleNamespace(settings=settings),
            emit=lambda t, p: events.append((t, p)),
            max_iters=6,
            compaction_keep_recent=1,
        )
        assert any(t == "context.compacted" for t, _ in events)
