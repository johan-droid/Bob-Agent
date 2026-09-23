"""Agentic tool-loop tests — LLM -> tool -> LLM with fallback (mocked).

Mandatory coverage:
- LLM requests get_test_value -> Bob executes -> result returns -> final answer.
- Two tool calls in one turn.
- Tool failure surfaced to the model, loop continues.
- Provider fallback during the tool loop preserves conversation/tool state.
- Successful responses are never resubmitted as retries (attempt identity).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from agent_system.services.agent_loop import run_tool_loop
from agent_system.services.fallback import invoke_with_fallback
from agent_system.services.model_router import InvocationResult
from agent_system.services.tools.protocol import parse_tool_calls
from agent_system.services.tools.registry import Tool, ToolContext, ToolRegistry


def _registry_with_test_tool(fail: bool = False) -> ToolRegistry:
    registry = ToolRegistry()

    def _handler(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        if fail:
            return {"error": "tool_error", "tool": "get_test_value", "detail": "boom"}
        return {"value": 42}

    registry.register(
        Tool(
            name="get_test_value",
            description="test tool",
            parameters={"type": "object", "properties": {}},
            risk="read",
            handler=_handler,
            scope="read",
        )
    )
    return registry


def _ctx() -> ToolContext:
    return ToolContext(settings=SimpleNamespace(tools_require_approval=False))


def _ok(model_id: str, provider: str, output: str, tool_calls: Any = None) -> InvocationResult:
    return InvocationResult(
        model_call_id="mc-1",
        model_id=model_id,
        provider=provider,
        ok=True,
        tokens_in=1,
        tokens_out=1,
        tokens_cached=0,
        usage_is_estimated=False,
        cost_usd=0.0,
        cost_is_estimated=False,
        latency_ms=1,
        output=output,
        tool_calls=tool_calls,
    )


class TestAgenticToolLoop:
    def test_single_tool_round_trip(self) -> None:
        """LLM -> tool call -> tool execution -> LLM -> final response."""
        calls: list[dict[str, Any]] = [
            {
                "output": "",
                "usage": {},
                "tool_calls": [{"id": "c1", "name": "get_test_value", "arguments": "{}"}],
            },
            {"output": "The value is 42.", "usage": {}},
        ]

        def invoke(prompt: str) -> dict[str, Any]:
            return calls.pop(0)

        loop = run_tool_loop(
            invoke=invoke,
            system="sys",
            task="What is the test value?",
            registry=_registry_with_test_tool(),
            ctx=_ctx(),
            max_iters=4,
        )
        assert loop.stopped == "done"
        assert "42" in loop.output
        assert loop.tool_calls == 1

    def test_two_tool_calls_one_turn(self) -> None:
        calls: list[dict[str, Any]] = [
            {
                "output": "",
                "usage": {},
                "tool_calls": [
                    {"id": "a", "name": "get_test_value", "arguments": "{}"},
                    {"id": "b", "name": "get_test_value", "arguments": "{}"},
                ],
            },
            {"output": "done twice", "usage": {}},
        ]

        def invoke(prompt: str) -> dict[str, Any]:
            return calls.pop(0)

        loop = run_tool_loop(
            invoke=invoke,
            system="s",
            task="t",
            registry=_registry_with_test_tool(),
            ctx=_ctx(),
            max_iters=4,
        )
        assert loop.stopped == "done"
        assert loop.tool_calls == 2

    def test_tool_failure_continues_loop(self) -> None:
        calls: list[dict[str, Any]] = [
            {
                "output": "",
                "usage": {},
                "tool_calls": [{"id": "c1", "name": "get_test_value", "arguments": "{}"}],
            },
            {"output": "recovered after tool error", "usage": {}},
        ]

        def invoke(prompt: str) -> dict[str, Any]:
            response = calls.pop(0)
            if not calls:
                # The tool result must have been injected into the transcript.
                assert "tool_result" in prompt or "tool_error" in prompt
            return response

        loop = run_tool_loop(
            invoke=invoke,
            system="s",
            task="t",
            registry=_registry_with_test_tool(fail=True),
            ctx=_ctx(),
            max_iters=4,
        )
        assert loop.stopped == "done"
        assert "recovered" in loop.output

    def test_malformed_tool_args_not_executed(self) -> None:
        response = {
            "output": "",
            "usage": {},
            "tool_calls": [{"id": "c1", "name": "get_test_value", "arguments": "{bad json"}],
        }
        calls = parse_tool_calls(response)
        assert len(calls) == 1 and calls[0].malformed is True


class TestToolLoopFallback:
    def test_fallback_preserves_tool_state(self) -> None:
        """A 500 on the primary falls back; tool state survives (same task)."""
        state = {"n": 0}

        class _Router:
            def invoke(self, _factory, model_id, _prompt, **_kw):
                state["n"] += 1
                if state["n"] == 1:
                    return InvocationResult(
                        model_call_id="x1",
                        model_id=model_id,
                        provider="groq",
                        ok=False,
                        tokens_in=None,
                        tokens_out=None,
                        tokens_cached=None,
                        usage_is_estimated=True,
                        cost_usd=None,
                        cost_is_estimated=True,
                        latency_ms=1,
                        error="HTTPStatusError: 500 boom",
                    )
                # Fallback provider returns the tool call; loop continues.
                return _ok(
                    model_id,
                    "gemini",
                    "",
                    [{"id": "c1", "name": "get_test_value", "arguments": "{}"}],
                )

        result = invoke_with_fallback(
            _Router(),
            None,
            [("groq", "openai/gpt-oss-20b"), ("gemini", "gemini-3.6-flash")],
            "hi",
            task_id="t1",
            worker_id="w1",
            max_attempts=2,
            health=None,
            bus=None,
        )
        assert result is not None and result.ok
        assert result.provider == "gemini"
        # Attempt identity: exactly 2 attempts, distinguishable providers.
        assert getattr(result, "fallback_attempts", None) is not None
        assert state["n"] == 2

    def test_success_never_resubmitted(self) -> None:
        """A successful response is returned once, never retried."""
        seen: list[str] = []

        class _Router:
            def invoke(self, _factory, model_id, _prompt, **_kw):
                seen.append(model_id)
                return _ok(model_id, "groq", "final")

        result = invoke_with_fallback(
            _Router(),
            None,
            [("groq", "m1"), ("groq", "m1"), ("groq", "m2")],
            "hi",
            max_attempts=3,
            health=None,
            bus=None,
        )
        assert result is not None and result.ok
        # Duplicate (groq, m1) candidate skipped — no retry storm, one response.
        assert seen == ["m1"]

    def test_full_loop_with_fallback_router(self) -> None:
        """End-to-end: fallback inside the tool loop across two turns."""
        turn = {"n": 0}

        def invoke(prompt: str) -> dict[str, Any]:
            turn["n"] += 1
            if turn["n"] == 1:
                # First turn: primary 500s, fallback returns tool call.
                class _R:
                    def __init__(self, attempt: int = 0):
                        self.attempt = attempt

                    def invoke(self, _f, model_id, _p, **_k):
                        self.attempt += 1
                        if self.attempt == 1:
                            return InvocationResult(
                                model_call_id="e1",
                                model_id=model_id,
                                provider="groq",
                                ok=False,
                                tokens_in=None,
                                tokens_out=None,
                                tokens_cached=None,
                                usage_is_estimated=True,
                                cost_usd=None,
                                cost_is_estimated=True,
                                latency_ms=1,
                                error="HTTPStatusError: 500 x",
                            )
                        return _ok(
                            model_id,
                            "gemini",
                            "",
                            [{"id": "c1", "name": "get_test_value", "arguments": "{}"}],
                        )

                res = invoke_with_fallback(
                    _R(),
                    None,
                    [("groq", "m1"), ("gemini", "m2")],
                    prompt,
                    max_attempts=2,
                    health=None,
                    bus=None,
                )
                assert res is not None and res.ok
                return {"output": res.output or "", "usage": {}, "tool_calls": res.tool_calls}
            return {"output": "The value is 42.", "usage": {}}

        loop = run_tool_loop(
            invoke=invoke,
            system="s",
            task="get value",
            registry=_registry_with_test_tool(),
            ctx=_ctx(),
            max_iters=4,
        )
        assert loop.stopped == "done" and "42" in loop.output
