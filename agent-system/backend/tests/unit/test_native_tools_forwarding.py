"""`tools=` must reach the provider on BOTH the streaming and non-streaming paths.

Regression: the payload used to be attached to ``kwargs`` *after* the
``invoke_streaming`` early return, so with the default
``ollama_streaming=true`` and an Ollama Cloud primary (is_primary=True) the
model never received a tool schema at all — native tool calling silently did
nothing on the path that production actually uses.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_system.services.inference_runtime import _call_once

TOOLS = [
    {
        "type": "function",
        "function": {"name": "research_search", "description": "d", "parameters": {}},
    }
]


class _Result:
    ok = True
    output = ""
    tool_calls = None
    provider = "nim"
    model_id = "m"
    latency_ms = 1
    tokens_in = tokens_out = 0


class _Router:
    """Captures the kwargs actually handed to the adapter boundary."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def invoke(self, factory: Any, model: str, prompt: str, **kwargs: Any) -> _Result:
        self.kwargs = kwargs
        return _Result()

    def invoke_streaming(
        self, factory: Any, model: str, prompt: str, on_token: Any = None, **kwargs: Any
    ) -> _Result:
        self.kwargs = kwargs
        return _Result()


@pytest.mark.parametrize("stream", [False, True])
def test_call_once_forwards_tools_on_both_paths(stream: bool) -> None:
    router = _Router()
    _call_once(
        router,
        None,
        "openai/gpt-oss-20b",
        "hi",
        session_id=None,
        task_id=None,
        agent_run_id=None,
        agent_type="llm",
        stream=stream,
        on_token=None,
        tools=TOOLS,
    )
    assert router.kwargs.get("tools") == TOOLS, (
        f"tools dropped on stream={stream} — native tool calling silently disabled"
    )


@pytest.mark.parametrize("stream", [False, True])
def test_call_once_omits_tools_when_not_supplied(stream: bool) -> None:
    """A no-tools call must not send an empty/None payload to the provider."""
    router = _Router()
    _call_once(
        router,
        None,
        "m",
        "hi",
        session_id=None,
        task_id=None,
        agent_run_id=None,
        agent_type="llm",
        stream=stream,
        on_token=None,
        tools=None,
    )
    assert "tools" not in router.kwargs
