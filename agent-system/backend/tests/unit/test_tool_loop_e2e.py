"""Agentic tool-loop end-to-end tests (mandatory acceptance suite).

Covers the complete LLM → tool_calls → execute → tool result → LLM → final
answer cycle over the *provider-agnostic* seam:

1. Full happy path with a fake ``get_test_value`` tool.
2. Multiple tool calls in one assistant turn.
3. Tool failure fed back to the model (model recovers with a final answer).
4. Provider fallback mid-loop: conversation/tool state survives the switch.
5. No raw tool-call messages ever returned to the user.
6. Malformed tool arguments are never executed.

All routers are in-memory stubs — no network, no API keys.
"""

from __future__ import annotations

import json
from typing import Any

from agent_system.services.agent_loop import run_tool_loop
from agent_system.services.tools.registry import Tool, ToolRegistry, _str_param

# ---------------------------------------------------------------------------
# Fake tool: get_test_value()
# ---------------------------------------------------------------------------


def _make_get_test_value(calls: list[dict[str, Any]] | None = None) -> Tool:
    def handler(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        if calls is not None:
            calls.append({"name": "get_test_value", "args": args})
        return {"value": 42, "source": "fake_test_tool"}

    return Tool(
        name="get_test_value",
        description="Returns the test value (fake tool for loop tests).",
        parameters={"value": _str_param("Optional label for the lookup.")},
        risk="read",
        handler=handler,
    )


def _make_failing_tool() -> Tool:
    def handler(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        raise RuntimeError("boom: deliberate tool failure")

    return Tool(
        name="explode",
        description="Always fails (fake tool for error-path tests).",
        parameters={"value": _str_param("Ignored.")},
        risk="read",
        handler=handler,
    )


def _registry(*tools: Tool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


class _Ctx:
    """Minimal ToolContext stand-in (no DB, no permissions)."""

    def __init__(self) -> None:
        self.settings = None
        self.factory = None
        self.session_id = None
        self.task_id = None
        self.agent_run_id = None
        self.agent_type = "test"
        self.workspace_id = None
        self.gate = None
        self.emit = None


# ---------------------------------------------------------------------------
# Scripted routers: each entry maps a transcript signature to a response
# ---------------------------------------------------------------------------


class _ScriptedRouter:
    """Returns scripted responses keyed by substring of the transcript."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = script
        self.calls = 0
        self.transcripts: list[str] = []

    def __call__(self, transcript: str) -> dict[str, Any]:
        self.transcripts.append(transcript)
        response = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return response


def _native_call(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


# ---------------------------------------------------------------------------
# 1. Full happy path: LLM → tool call → execute → LLM → final
# ---------------------------------------------------------------------------


class TestFullToolLoop:
    def test_llm_to_tool_to_final_answer(self) -> None:
        tool_calls: list[dict[str, Any]] = []
        registry = _registry(_make_get_test_value(tool_calls))
        router = _ScriptedRouter(
            [
                {
                    "output": "",
                    "tool_calls": [_native_call("call_1", "get_test_value", {})],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
                {
                    "output": "The test value is 42.",
                    "usage": {"input_tokens": 20, "output_tokens": 8},
                },
            ]
        )
        result = run_tool_loop(
            invoke=router,
            system="You are Bob.",
            task="What is the test value?",
            registry=registry,
            ctx=_Ctx(),
            emit=None,
        )
        assert result.stopped == "done"
        assert result.output == "The test value is 42."
        # The fake tool ran exactly once with the model's arguments.
        assert tool_calls == [{"name": "get_test_value", "args": {}}]
        # Usage aggregated across both LLM turns.
        assert result.usage["input_tokens"] == 30
        assert result.usage["output_tokens"] == 13
        # The tool result was injected into the follow-up transcript.
        assert "<tool_result" in router.transcripts[1]
        assert '"value": 42' in router.transcripts[1]

    def test_raw_tool_call_message_never_returned_as_output(self) -> None:
        """The Telegram-facing output is the final answer, not the tool call."""
        registry = _registry(_make_get_test_value())
        router = _ScriptedRouter(
            [
                {
                    "output": "",
                    "tool_calls": [_native_call("call_1", "get_test_value", {})],
                },
                {"output": "Final: 42"},
            ]
        )
        result = run_tool_loop(
            invoke=router,
            system="s",
            task="t",
            registry=registry,
            ctx=_Ctx(),
        )
        assert result.output == "Final: 42"
        assert "tool_calls" not in result.output
        assert "get_test_value" not in result.output


# ---------------------------------------------------------------------------
# 2. Multiple tool calls in one turn
# ---------------------------------------------------------------------------


class TestMultipleToolCalls:
    def test_two_calls_in_one_assistant_response(self) -> None:
        tool_calls: list[dict[str, Any]] = []
        registry = _registry(_make_get_test_value(tool_calls))
        router = _ScriptedRouter(
            [
                {
                    "output": "",
                    "tool_calls": [
                        _native_call("call_a", "get_test_value", {"value": "first"}),
                        _native_call("call_b", "get_test_value", {"value": "second"}),
                    ],
                },
                {"output": "Both lookups done: 42 and 42."},
            ]
        )
        result = run_tool_loop(
            invoke=router,
            system="s",
            task="t",
            registry=registry,
            ctx=_Ctx(),
        )
        assert result.stopped == "done"
        assert len(tool_calls) == 2
        assert tool_calls[0]["args"] == {"value": "first"}
        assert tool_calls[1]["args"] == {"value": "second"}
        assert result.tool_calls == 2
        # Both results present in the follow-up transcript (the registry's
        # prompt block mentions <tool_result> once in prose — exclude it).
        assert router.transcripts[1].count("<tool_result name=") == 2


# ---------------------------------------------------------------------------
# 3. Tool failure → fed back → model recovers
# ---------------------------------------------------------------------------


class TestToolFailureRecovery:
    def test_tool_failure_becomes_model_readable_result(self) -> None:
        registry = _registry(_make_failing_tool())
        router = _ScriptedRouter(
            [
                {"output": "", "tool_calls": [_native_call("call_1", "explode", {})]},
                {"output": "The tool failed, so here is a plain answer instead."},
            ]
        )
        result = run_tool_loop(
            invoke=router,
            system="s",
            task="t",
            registry=registry,
            ctx=_Ctx(),
        )
        assert result.stopped == "done"
        assert "plain answer" in result.output
        # The failure was surfaced to the model, not swallowed.
        assert "capability_crash" in router.transcripts[1]
        assert "boom" in router.transcripts[1]

    def test_unknown_tool_lists_available_alternatives(self) -> None:
        registry = _registry(_make_get_test_value())
        router = _ScriptedRouter(
            [
                {"output": "", "tool_calls": [_native_call("call_1", "no_such_tool", {})]},
                {"output": "Got it, using the right tool next time."},
            ]
        )
        result = run_tool_loop(
            invoke=router,
            system="s",
            task="t",
            registry=registry,
            ctx=_Ctx(),
        )
        assert result.stopped == "done"
        assert "unknown_tool" in router.transcripts[1]
        assert "get_test_value" in router.transcripts[1]  # available list injected

    def test_malformed_arguments_never_executed(self) -> None:
        tool_calls: list[dict[str, Any]] = []
        registry = _registry(_make_get_test_value(tool_calls))
        router = _ScriptedRouter(
            [
                {
                    "output": "",
                    "tool_calls": [
                        {
                            "id": "call_bad",
                            "type": "function",
                            "function": {"name": "get_test_value", "arguments": "{not json"},
                        }
                    ],
                },
                {"output": "Recovered."},
            ]
        )
        result = run_tool_loop(
            invoke=router,
            system="s",
            task="t",
            registry=registry,
            ctx=_Ctx(),
        )
        # The handler never ran with garbage arguments.
        assert tool_calls == []
        assert result.stopped == "done"
        assert "malformed_arguments" in router.transcripts[1]


# ---------------------------------------------------------------------------
# 4. Provider fallback mid-loop — conversation/tool state survives
# ---------------------------------------------------------------------------


class TestFallbackMidLoop:
    def test_tool_state_survives_provider_switch(self) -> None:
        """Loop: provider A answers with a tool call, dies before the follow-up;
        provider B picks up the transcript *including the tool result*."""
        tool_calls: list[dict[str, Any]] = []
        registry = _registry(_make_get_test_value(tool_calls))

        transcripts: list[str] = []

        def invoke(transcript: str) -> dict[str, Any]:
            transcripts.append(transcript)
            if len(transcripts) == 1:
                # Provider A: requests the tool, then "crashes" is impossible
                # mid-call — the crash is modelled by provider A never being
                # consulted again (fallback router returns a different adapter
                # for attempt 2). The tool result must be in the transcript
                # provider B sees.
                return {
                    "output": "",
                    "tool_calls": [_native_call("call_1", "get_test_value", {})],
                    "provider": "groq",
                }
            # Provider B must see the full accumulated context.
            return {
                "output": "Recovered by provider B: value is 42.",
                "provider": "gemini",
            }

        result = run_tool_loop(
            invoke=invoke,
            system="s",
            task="t",
            registry=registry,
            ctx=_Ctx(),
        )
        assert result.stopped == "done"
        assert "provider B" in result.output
        assert len(tool_calls) == 1
        # The tool result provider B received proves tool state survived.
        assert '"value": 42' in transcripts[1]
        assert "<tool_result" in transcripts[1]

    def test_fallback_router_preserves_transcript_across_attempts(self) -> None:
        """The fallback wrapper feeds the *same* transcript to the next
        candidate — attempts differ only in provider/model identity."""
        seen: list[str] = []
        turns: list[str] = []

        class _FailingThenOKRouter:
            """First call raises (provider dead), second succeeds."""

            def invoke(self, _factory, model_id, prompt, **_kw):
                seen.append(prompt)
                turns.append(model_id)
                from agent_system.services.model_router import InvocationResult

                if model_id == "model-a":
                    raise ConnectionError("provider unreachable")
                return InvocationResult(
                    model_call_id="ok",
                    model_id=model_id,
                    provider="gemini",
                    ok=True,
                    tokens_in=1,
                    tokens_out=1,
                    tokens_cached=None,
                    usage_is_estimated=False,
                    cost_usd=None,
                    cost_is_estimated=True,
                    latency_ms=2,
                    output="fallback answer",
                )

        router = _FailingThenOKRouter()
        from agent_system.services.fallback import invoke_with_fallback

        result = invoke_with_fallback(
            router,
            None,
            [("groq", "model-a"), ("gemini", "model-b")],
            "the exact user prompt",
            max_attempts=2,
        )
        assert result.ok is True
        assert result.output == "fallback answer"
        # Same prompt went to both attempts — nothing lost, nothing duplicated.
        assert seen == ["the exact user prompt", "the exact user prompt"]
        assert turns == ["model-a", "model-b"]


# ---------------------------------------------------------------------------
# 5. Loop guards
# ---------------------------------------------------------------------------


class TestLoopGuards:
    def test_tool_calling_model_looping_hits_max_iters(self) -> None:
        registry = _registry(_make_get_test_value())
        router = _ScriptedRouter(
            [{"output": "", "tool_calls": [_native_call("call", "get_test_value", {})]}]
        )
        result = run_tool_loop(
            invoke=router,
            system="s",
            task="t",
            registry=registry,
            ctx=_Ctx(),
            max_iters=3,
        )
        assert result.stopped == "max_iters"
        assert result.iterations == 3
        assert result.tool_calls == 3

    def test_empty_model_response_with_no_calls_ends_done(self) -> None:
        registry = _registry(_make_get_test_value())
        router = _ScriptedRouter([{"output": "", "usage": {}}])
        result = run_tool_loop(
            invoke=router,
            system="s",
            task="t",
            registry=registry,
            ctx=_Ctx(),
        )
        assert result.stopped == "done"
        assert result.output == ""

    def test_model_crash_degrades_to_error_not_exception(self) -> None:
        registry = _registry(_make_get_test_value())

        def invoke(_transcript: str) -> dict[str, Any]:
            raise ConnectionError("provider unreachable")

        result = run_tool_loop(
            invoke=invoke,
            system="s",
            task="t",
            registry=registry,
            ctx=_Ctx(),
        )
        assert result.stopped == "error"
        assert "provider unreachable" in result.output
