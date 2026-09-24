"""Provider compatibility matrix — one lifecycle, every adapter, no bypass.

Proves the RC-phase invariant: every supported provider produces tool calls
that survive the *identical* execution lifecycle, routed through a single
physical handler-invocation seam (``tools/execution._invoke_handler``), with
no provider-specific execution branches.

Layers covered:

1. **Adapter normalization** — every provider's native wire shape yields the
   identical canonical ``tool_calls`` carrier ``[{"id", "name", "arguments"}]``
   from its adapter (Completeness guard: a new provider without a wire payload
   fails ``TestMatrixCompleteness``).
2. **Protocol parse parity** — the canonical carrier and the fenced-text
   protocol parse into the same ``ToolCall`` fields (name, arguments; protocol
   kind differs by design).
3. **Single-seam lifecycle** — ``_invoke_handler`` is entered *exactly once*
   per parsed call regardless of protocol origin (native vs fenced); the
   handler is never bypassed or multiplied.
4. **Router carrier** — both ``ModelRouter.invoke`` and
   ``ModelRouter.invoke_streaming`` forward the adapter's canonical
   ``tool_calls`` into ``InvocationResult.tool_calls`` for every transport.
5. **SSE streaming assembly** — OpenAI-compatible ``delta.tool_calls`` and
   Anthropic ``content_block_delta`` (``partial_json``) fragments reassemble
   into the identical canonical carrier once the generator is exhausted.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent_system.config import Settings
from agent_system.services.model_router import (
    EchoProvider,
    ModelInfo,
    ModelRegistry,
    ModelRouter,
    PricingRegistry,
    ProviderAdapter,
)
from agent_system.services.providers import (
    PROVIDERS,
    AnthropicAdapter,
    GeminiAdapter,
    OpenAICompatibleAdapter,
    build_adapter,
)
from agent_system.services.tools import execution
from agent_system.services.tools.execution import run_tool_call
from agent_system.services.tools.protocol import (
    ProtocolKind,
    ToolCall,
    parse_tool_calls,
)
from agent_system.services.tools.registry import Tool, ToolContext, ToolRegistry

# ---------------------------------------------------------------------------
# Canonical test call + provider-native wire payloads
# ---------------------------------------------------------------------------

_CALL_NAME = "get_user"
_CALL_ARGS: dict[str, str] = {"id": "u-42"}
_EXPECTED_CANONICAL_CALL: dict[str, Any] = {
    "id": "call_abc",
    "name": _CALL_NAME,
    "arguments": json.dumps(_CALL_ARGS),
}

_OPENAI_NATIVE_PAYLOAD: dict[str, Any] = {
    "choices": [
        {
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {"name": _CALL_NAME, "arguments": json.dumps(_CALL_ARGS)},
                    }
                ],
            }
        }
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 10},
}

_GEMINI_NATIVE_PAYLOAD: dict[str, Any] = {
    "candidates": [
        {"content": {"parts": [{"functionCall": {"name": _CALL_NAME, "args": _CALL_ARGS}}]}}
    ],
    "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 10},
}

_ANTHROPIC_NATIVE_PAYLOAD: dict[str, Any] = {
    "content": [
        {"type": "text", "text": "I'll fetch that."},
        {"type": "tool_use", "id": "call_abc", "name": _CALL_NAME, "input": _CALL_ARGS},
    ],
    "usage": {"input_tokens": 5, "output_tokens": 10},
}

#: Wire payload keyed by provider key — every row in the matrix is exercised.
#: The completeness guard keeps this in lock-step with the catalog.
EXPECTED_WIRE: dict[str, dict[str, Any]] = {
    # OpenAI-compatible transports share the same wire shape.
    "openai": _OPENAI_NATIVE_PAYLOAD,
    "groq": _OPENAI_NATIVE_PAYLOAD,
    "ollama": _OPENAI_NATIVE_PAYLOAD,
    "openrouter": _OPENAI_NATIVE_PAYLOAD,
    "together": _OPENAI_NATIVE_PAYLOAD,
    "mistral": _OPENAI_NATIVE_PAYLOAD,
    "deepseek": _OPENAI_NATIVE_PAYLOAD,
    "huggingface": _OPENAI_NATIVE_PAYLOAD,
    "tokenrouter": _OPENAI_NATIVE_PAYLOAD,
    "nim": _OPENAI_NATIVE_PAYLOAD,
    "ollama_cloud": _OPENAI_NATIVE_PAYLOAD,
    "opencode": _OPENAI_NATIVE_PAYLOAD,
    # Native transports.
    "gemini": _GEMINI_NATIVE_PAYLOAD,
    "anthropic": _ANTHROPIC_NATIVE_PAYLOAD,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {}
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _make_response_mock(payload: dict[str, Any]) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def _patch_client(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> MagicMock:
    """Patch httpx.Client so `.post` returns a mock holding the request kwargs."""
    client = MagicMock()
    client.post.return_value = _make_response_mock(payload)
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    monkeypatch.setattr(
        "agent_system.services.providers.httpx.Client",
        MagicMock(return_value=client),
    )
    return client.post


def _sqlite_env(tmp_path: Any) -> tuple[Any, Any, ModelRouter, Any]:
    """SQLite-backed (factory, bus, router, engine) for the router carrier tests."""
    from agent_system.infra.db import make_engine, make_session_factory
    from agent_system.infra.event_bus import EventBus
    from agent_system.infra.models import Base

    engine = make_engine(f"sqlite:///{tmp_path / 'compat_matrix.db'}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    bus = EventBus()
    router = ModelRouter(bus, pricing=PricingRegistry(), registry=ModelRegistry())
    return factory, bus, router, engine


def _register_provider(router: ModelRouter, provider_key: str) -> None:
    """Register the provider's adapter + an entry for its default model."""
    spec = PROVIDERS[provider_key]
    adapter = build_adapter(provider_key, _settings(), api_key="x")
    assert adapter is not None, f"no adapter for provider {provider_key!r}"
    router.register_adapter(provider_key, adapter)
    router.pricing.register(
        ModelInfo(
            model_id=spec.default_model,
            provider=provider_key,
            input_cost_per_1m=0.0,
            output_cost_per_1m=0.0,
            context_window=128_000,
        )
    )


def _fenced_text(name: str, arguments: dict[str, Any]) -> str:
    return f"```tool:{name}\n{json.dumps(arguments)}\n```"


# ---------------------------------------------------------------------------
# Layer 0 — completeness guard
# ---------------------------------------------------------------------------


class TestMatrixCompleteness:
    """Every provider in the catalog must have an explicit matrix row."""

    def test_all_providers_covered(self) -> None:
        assert EXPECTED_WIRE.keys() == set(PROVIDERS.keys())

    def test_openai_compat_share_the_shared_wire_shape(self) -> None:
        adapter = OpenAICompatibleAdapter(PROVIDERS["openai"], api_key="x")
        for key in EXPECTED_WIRE:
            if isinstance(build_adapter(key, _settings(), api_key="x"), type(adapter)):
                assert EXPECTED_WIRE[key] is _OPENAI_NATIVE_PAYLOAD, (
                    f"{key} is OpenAI-compatible but has a provider-specific wire payload"
                )


# ---------------------------------------------------------------------------
# Layer 1 — adapter normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider_key", sorted(EXPECTED_WIRE.keys()))
class TestAdapterCanonicalToolCalls:
    """Every provider adapter emits the identical canonical tool_calls list."""

    def test_invoke_normalizes_native_wire(
        self, monkeypatch: pytest.MonkeyPatch, provider_key: str
    ) -> None:
        _patch_client(monkeypatch, EXPECTED_WIRE[provider_key])
        adapter = build_adapter(provider_key, _settings(), api_key="x")
        assert adapter is not None

        result = adapter.invoke(PROVIDERS[provider_key].default_model, "hi")

        calls = result.get("tool_calls")
        assert isinstance(calls, list), f"{provider_key}: expected a tool_calls list"
        assert len(calls) == 1, f"{provider_key}: expected exactly one call"
        assert calls[0]["name"] == _CALL_NAME
        assert calls[0]["arguments"] == json.dumps(_CALL_ARGS)
        assert set(calls[0]) == {"id", "name", "arguments"}


# ---------------------------------------------------------------------------
# Layer 2 — protocol parse parity
# ---------------------------------------------------------------------------


class TestProtocolParseParity:
    """Native canonical carrier and fenced text produce the same ToolCall fields."""

    def test_native_matches_fenced(self) -> None:
        native_calls = parse_tool_calls({"output": "", "tool_calls": [_EXPECTED_CANONICAL_CALL]})
        assert len(native_calls) == 1
        native = native_calls[0]

        fenced_calls = parse_tool_calls({"output": _fenced_text(_CALL_NAME, _CALL_ARGS)})
        assert len(fenced_calls) == 1
        fenced = fenced_calls[0]

        assert native.name == fenced.name == _CALL_NAME
        assert native.arguments == fenced.arguments == _CALL_ARGS
        assert native.protocol == ProtocolKind.NATIVE
        assert fenced.protocol == ProtocolKind.FENCED

    def test_native_flat_shape_with_dict_arguments(self) -> None:
        calls = parse_tool_calls(
            {"output": "", "tool_calls": [{"id": "x1", "name": "fn", "arguments": {"a": 1}}]}
        )
        assert len(calls) == 1
        assert calls[0].arguments == {"a": 1}


# ---------------------------------------------------------------------------
# Layer 3 — single-seam lifecycle
# ---------------------------------------------------------------------------


class TestSingleSeamLifecycle:
    """Native and fenced ToolCalls enter _invoke_handler exactly once each."""

    def _registry_and_ctx(self) -> tuple[ToolRegistry, ToolContext]:
        registry = ToolRegistry()
        registry.register(
            Tool(
                name=_CALL_NAME,
                description="stub",
                parameters={
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                },
                risk="read",
                handler=lambda a, c: {"value": a.get("id")},
                scope="read",
            )
        )
        ctx = ToolContext(settings=SimpleNamespace(tools_require_approval=False))
        return registry, ctx

    def test_native_and_fenced_use_the_same_seam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        handler_calls: list[tuple[str, dict[str, Any]]] = []

        def spy(tool: Tool, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            handler_calls.append((tool.name, dict(args)))
            return {"value": args.get("id")}

        monkeypatch.setattr(execution, "_invoke_handler", spy)

        registry, ctx = self._registry_and_ctx()
        for protocol, source in (
            (ProtocolKind.NATIVE, "provider_tool_call"),
            (ProtocolKind.FENCED, "assistant_text"),
        ):
            call = ToolCall(
                id=f"call_{protocol.value}",
                name=_CALL_NAME,
                arguments=_CALL_ARGS,
                source=source,
                protocol=protocol,
            )
            result = run_tool_call(call, registry, ctx)
            assert result.ok

        # Exactly one handler entry per call, identical arguments, no bypass.
        assert handler_calls == [
            (_CALL_NAME, _CALL_ARGS),
            (_CALL_NAME, _CALL_ARGS),
        ]


# ---------------------------------------------------------------------------
# Layer 4 — router carrier
# ---------------------------------------------------------------------------


class TestRouterCarrier:
    """ModelRouter forwards adapter tool_calls into InvocationResult."""

    @pytest.mark.parametrize("provider_key", sorted(EXPECTED_WIRE.keys()))
    def test_invoke_carries_tool_calls(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any, provider_key: str
    ) -> None:
        factory, _bus, router, engine = _sqlite_env(tmp_path)
        try:
            _patch_client(monkeypatch, EXPECTED_WIRE[provider_key])
            _register_provider(router, provider_key)
            model_id = PROVIDERS[provider_key].default_model

            result = router.invoke(factory, model_id, "hi", session_id="ses_matrix_invoke")

            assert result.ok
            assert isinstance(result.tool_calls, list)
            assert len(result.tool_calls) == 1
            assert result.tool_calls[0]["name"] == _CALL_NAME
            assert json.loads(result.tool_calls[0]["arguments"]) == _CALL_ARGS
        finally:
            engine.dispose()

    def test_streaming_carries_tool_calls(self, tmp_path: Any) -> None:
        factory, _bus, router, engine = _sqlite_env(tmp_path)
        try:
            router.pricing.register(
                ModelInfo(
                    model_id="stub-stream",
                    provider="stub",
                    input_cost_per_1m=0.0,
                    output_cost_per_1m=0.0,
                )
            )

            class _StreamAdapter(ProviderAdapter):
                supports_streaming = True

                def invoke(self, *_a: Any, **_kw: Any) -> dict[str, Any]:
                    raise AssertionError("invoke must not be called")

                def stream(self, *_a: Any, **_kw: Any) -> Any:
                    def _gen() -> Any:
                        yield "partial"

                    return (
                        _gen(),
                        {"input_tokens": 1, "output_tokens": 2},
                        [dict(_EXPECTED_CANONICAL_CALL)],
                    )

            router.register_adapter("stub", _StreamAdapter())
            result = router.invoke_streaming(
                factory, "stub-stream", "hi", session_id="ses_matrix_stream"
            )
            assert result.ok
            assert result.output == "partial"
            assert result.tool_calls == [dict(_EXPECTED_CANONICAL_CALL)]
        finally:
            engine.dispose()

    def test_echo_streaming_has_no_tool_calls(self, tmp_path: Any) -> None:
        factory, _bus, router, engine = _sqlite_env(tmp_path)
        try:
            router.pricing.register(
                ModelInfo(
                    model_id="echo-stream",
                    provider="echo",
                    input_cost_per_1m=0.0,
                    output_cost_per_1m=0.0,
                )
            )
            router.register_adapter("echo", EchoProvider())
            result = router.invoke_streaming(
                factory, "echo-stream", "hi", session_id="ses_matrix_echo"
            )
            assert result.ok
            assert result.tool_calls is None
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# Layer 5 — SSE streaming assembly
# ---------------------------------------------------------------------------


class TestSSEStreamingAssembly:
    """Fragmented SSE tool-call chunks reassemble into the canonical carrier."""

    def _mock_client_stream(self, monkeypatch: pytest.MonkeyPatch, sse_lines: list[str]) -> None:
        resp = MagicMock()
        resp.iter_lines.return_value = iter(sse_lines)
        resp.raise_for_status.return_value = None
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        client.stream.return_value = resp
        monkeypatch.setattr(
            "agent_system.services.providers.httpx.Client",
            MagicMock(return_value=client),
        )

    def test_openai_streaming_assembles_fragmented_tool_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sse_lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_s1",'
            '"type":"function","function":{"name":"get_user","arguments":""}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
            '"function":{"arguments":"{\\"id\\": \\"u-"}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
            '"function":{"arguments":"42\\"}"}}]}}]}',
            'data: {"choices":[{"delta":{"content":"ok"}}],'
            '"usage":{"prompt_tokens":1,"completion_tokens":2}}',
        ]
        self._mock_client_stream(monkeypatch, sse_lines)

        adapter = OpenAICompatibleAdapter(PROVIDERS["openai"], api_key="x")
        chunks, usage, tool_calls = adapter.stream("gpt-4o-mini", "hi")
        output = "".join(chunks)

        assert output == "ok"
        assert usage["input_tokens"] == 1
        assert usage["output_tokens"] == 2
        assert isinstance(tool_calls, list)
        assert len(tool_calls) == 1
        assert tool_calls[0] == {
            "id": "call_s1",
            "name": _CALL_NAME,
            "arguments": json.dumps(_CALL_ARGS),
        }

    def test_openai_streaming_without_tool_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sse_lines = [
            'data: {"choices":[{"delta":{"content":"just text"}}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}',
        ]
        self._mock_client_stream(monkeypatch, sse_lines)

        adapter = OpenAICompatibleAdapter(PROVIDERS["openai"], api_key="x")
        chunks, _usage, tool_calls = adapter.stream("gpt-4o-mini", "hi")
        assert "".join(chunks) == "just text"
        assert tool_calls == []

    def test_anthropic_streaming_assembles_tool_use(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sse_lines = [
            "event: message_start",
            'data: {"type":"message_start","message":{"usage":{"input_tokens":3}}}',
            "event: content_block_start",
            'data: {"type":"content_block_start","index":0,"content_block":'
            '{"type":"tool_use","id":"toolu_01","name":"get_user"}}',
            "event: content_block_delta",
            'data: {"type":"content_block_delta","index":0,"delta":'
            '{"type":"input_json_delta","partial_json":"{\\"id\\": \\"u"}}',
            "event: content_block_delta",
            'data: {"type":"content_block_delta","index":0,"delta":'
            '{"type":"input_json_delta","partial_json":"-42\\"}"}}',
            "event: content_block_stop",
            'data: {"type":"content_block_stop","index":0}',
            "event: content_block_start",
            'data: {"type":"content_block_start","index":1,"content_block":'
            '{"type":"text","text":""}}',
            "event: content_block_delta",
            'data: {"type":"content_block_delta","index":1,"delta":'
            '{"type":"text_delta","text":"done"}}',
            "event: content_block_stop",
            'data: {"type":"content_block_stop","index":1}',
            "event: message_delta",
            'data: {"type":"message_delta","usage":{"output_tokens":4}}',
        ]
        self._mock_client_stream(monkeypatch, sse_lines)

        adapter = AnthropicAdapter(PROVIDERS["anthropic"], api_key="x")
        chunks, usage, tool_calls = adapter.stream("claude-sonnet-4-5", "hi")

        assert "".join(chunks) == "done"
        assert usage["input_tokens"] == 3
        assert usage["output_tokens"] == 4
        assert isinstance(tool_calls, list)
        assert len(tool_calls) == 1
        assert tool_calls[0] == {
            "id": "toolu_01",
            "name": _CALL_NAME,
            "arguments": json.dumps(_CALL_ARGS),
        }

    def test_gemini_streaming_assembles_text_and_function_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        evt1 = json.dumps(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "working "},
                                {
                                    "functionCall": {
                                        "name": _CALL_NAME,
                                        "args": _CALL_ARGS,
                                    }
                                },
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3},
            }
        )
        evt2 = json.dumps(
            {"candidates": [{"content": {"parts": [{"text": "done"}]}, "finishReason": "STOP"}]}
        )
        self._mock_client_stream(monkeypatch, [f"data: {evt1}", f"data: {evt2}"])

        adapter = GeminiAdapter(PROVIDERS["gemini"], api_key="x")
        chunks, usage, tool_calls = adapter.stream("gemini-3.6-flash", "hi")

        assert "".join(chunks) == "working done"
        assert usage == {"input_tokens": 2, "output_tokens": 3}
        assert isinstance(tool_calls, list)
        assert len(tool_calls) == 1
        assert tool_calls[0] == {
            "id": "call_native_1",
            "name": _CALL_NAME,
            "arguments": json.dumps(_CALL_ARGS),
        }

    def test_anthropic_streaming_without_tool_use(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sse_lines = [
            "event: message_start",
            'data: {"type":"message_start","message":{"usage":{}}}',
            "event: content_block_start",
            'data: {"type":"content_block_start","index":0,'
            '"content_block":{"type":"text","text":""}}',
            "event: content_block_delta",
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"hi"}}',
            "event: content_block_stop",
            'data: {"type":"content_block_stop","index":0}',
            "event: message_delta",
            'data: {"type":"message_delta","usage":{"output_tokens":1}}',
        ]
        self._mock_client_stream(monkeypatch, sse_lines)

        adapter = AnthropicAdapter(PROVIDERS["anthropic"], api_key="x")
        chunks, _usage, tool_calls = adapter.stream("claude-sonnet-4-5", "hi")
        assert "".join(chunks) == "hi"
        assert tool_calls == []
