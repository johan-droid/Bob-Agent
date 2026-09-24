"""Unit tests — real LLM provider adapters (mock httpx)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from agent_system.config import Settings
from agent_system.services.providers import (
    PROVIDERS,
    AnthropicAdapter,
    GeminiAdapter,
    OpenAICompatibleAdapter,
    build_adapter,
    build_pricing,
    configured_providers,
    default_model_id,
)
from agent_system.services.providers import (
    test_provider as check_provider,
)


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


def _patch_client(monkeypatch, response_payload: dict[str, Any]) -> Any:
    """Patch httpx.Client so `.post` returns a mock holding the request kwargs."""
    client = MagicMock()
    client.post.return_value = _make_response_mock(response_payload)
    # Adapters use `with httpx.Client(...) as client:` — make __enter__ return itself.
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    monkeypatch.setattr(
        "agent_system.services.providers.httpx.Client", MagicMock(return_value=client)
    )
    return client.post


class TestProviderSpec:
    def test_all_providers_have_spec(self) -> None:
        assert len(PROVIDERS) >= 14
        for key in (
            "groq",
            "ollama",
            "openrouter",
            "gemini",
            "anthropic",
            "tokenrouter",
        ):
            assert key in PROVIDERS

    def test_spec_fields(self) -> None:
        for key, spec in PROVIDERS.items():
            assert spec.key == key
            assert isinstance(spec.label, str)
            assert isinstance(spec.base_url, str)


class TestGroqRegression:
    def test_groq_url_construction_prevents_404_double_slash(self, monkeypatch) -> None:
        """Regression test: Groq base URL with trailing slash or chat/completions is sanitized."""
        post = _patch_client(
            monkeypatch,
            {
                "choices": [{"message": {"content": "pong"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )
        s = _settings(groq_base_url="https://api.groq.com/openai/v1/chat/completions/")
        adapter = build_adapter("groq", s, api_key="test-key")
        assert adapter is not None
        result = adapter.invoke("openai/gpt-oss-20b", "ping")

        assert result["output"] == "pong"
        args, kwargs = post.call_args
        url: str = args[0]
        assert url == "https://api.groq.com/openai/v1/chat/completions"

    def test_groq_base_url_variations_sanitized(self) -> None:
        from agent_system.services.providers import _sanitize_base_url

        assert _sanitize_base_url("https://api.groq.com") == "https://api.groq.com/openai/v1"
        assert _sanitize_base_url("https://api.groq.com/v1") == "https://api.groq.com/openai/v1"
        assert (
            _sanitize_base_url("https://api.groq.com/chat/completions")
            == "https://api.groq.com/openai/v1"
        )
        assert (
            _sanitize_base_url("https://api.groq.com/openai/v1/chat/completions/")
            == "https://api.groq.com/openai/v1"
        )

    def test_permanent_404_error_quarantines_provider(self) -> None:
        from agent_system.services.provider_health import (
            ProviderHealth,
            ProviderHealthTracker,
            classify_provider_error,
            is_provider_failure,
        )

        err = "HTTPStatusError: 404 Not Found at https://api.groq.com/openai/v1/chat/completions"
        assert is_provider_failure(err) is True
        assert classify_provider_error(err) == ProviderHealth.DISABLED

        tracker = ProviderHealthTracker()
        tracker.report_failure("groq", "openai/gpt-oss-20b", err)
        assert tracker.is_routable("groq", "openai/gpt-oss-20b") is False


class TestOpenAICompat:
    def test_invoke_sends_messages(self, monkeypatch) -> None:
        post = _patch_client(
            monkeypatch,
            {
                "choices": [{"message": {"content": "pong"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )
        adapter = OpenAICompatibleAdapter(PROVIDERS["groq"], api_key="test-key")
        result = adapter.invoke("openai/gpt-oss-20b", "ping")

        assert result["output"] == "pong"
        assert result["usage"]["input_tokens"] == 1
        _, kwargs = post.call_args
        headers = kwargs["headers"]
        body = kwargs["json"]
        assert headers["Authorization"] == "Bearer test-key"
        assert body["model"] == "openai/gpt-oss-20b"
        assert body["messages"][0]["role"] == "user"
        assert body["messages"][0]["content"] == "ping"

    def test_invoke_no_api_key_no_auth_header(self, monkeypatch) -> None:
        post = _patch_client(monkeypatch, {"choices": [{"message": {"content": ""}}], "usage": {}})
        adapter = OpenAICompatibleAdapter(PROVIDERS["ollama"], api_key=None)
        adapter.invoke("llama3.2", "hi")
        _, kwargs = post.call_args
        assert "Authorization" not in kwargs["headers"]

    def test_extra_headers_passed(self, monkeypatch) -> None:
        post = _patch_client(monkeypatch, {"choices": [{"message": {"content": ""}}], "usage": {}})
        adapter = OpenAICompatibleAdapter(
            PROVIDERS["groq"], api_key="k", extra_headers={"X-Custom": "val"}
        )
        adapter.invoke("openai/gpt-oss-20b", "hi")
        _, kwargs = post.call_args
        assert kwargs["headers"].get("X-Custom") == "val"


class TestGemini:
    def test_invoke_uses_x_goog_api_key(self, monkeypatch) -> None:
        post = _patch_client(
            monkeypatch,
            {
                "candidates": [{"content": {"parts": [{"text": "g hello"}]}}],
                "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
            },
        )
        adapter = GeminiAdapter(PROVIDERS["gemini"], api_key="g-key")
        result = adapter.invoke("gemini-3.6-flash", "hello")

        assert result["output"] == "g hello"
        args, kwargs = post.call_args
        url: str = args[0]
        headers = kwargs["headers"]
        assert headers["x-goog-api-key"] == "g-key"
        assert "models/gemini-3.6-flash:generateContent" in url

    def test_invoke_api_key_as_query_param(self, monkeypatch) -> None:
        body = {"candidates": [{"content": {"parts": [{"text": ""}]}}]}
        post = _patch_client(monkeypatch, body)
        adapter = GeminiAdapter(PROVIDERS["gemini"], api_key=None)
        adapter.invoke("gemini-3.6-flash", "hi")
        args, kwargs = post.call_args
        assert "x-goog-api-key" not in (kwargs.get("headers") or {})


class TestStreamOptions:
    """`stream_options.include_usage` is opt-in: Groq/OpenRouter gateways 400 on it."""

    def _patch_stream(self, monkeypatch, sse_lines: list[str]) -> dict[str, Any]:
        """Patch httpx.Client.stream; capture the outgoing JSON payload."""
        captured: dict[str, Any] = {}
        resp = MagicMock()
        resp.iter_lines.return_value = iter(sse_lines)
        resp.raise_for_status.return_value = None
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False

        def _stream(method: str, url: str, headers: Any = None, json: Any = None) -> Any:
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return resp

        client.stream.side_effect = _stream
        monkeypatch.setattr(
            "agent_system.services.providers.httpx.Client",
            MagicMock(return_value=client),
        )
        return captured

    def test_groq_stream_omits_stream_options(self, monkeypatch) -> None:
        captured = self._patch_stream(
            monkeypatch, ['data: {"choices":[{"delta":{"content":"hi"}}]}']
        )
        adapter = OpenAICompatibleAdapter(PROVIDERS["groq"], api_key="k")
        chunks, usage, tool_calls = adapter.stream("openai/gpt-oss-20b", "hi")

        assert "".join(chunks) == "hi"
        assert usage == {}
        assert tool_calls == []
        assert "stream_options" not in captured["json"]
        assert captured["json"]["stream"] is True

    def test_openrouter_stream_omits_stream_options(self, monkeypatch) -> None:
        captured = self._patch_stream(
            monkeypatch, ['data: {"choices":[{"delta":{"content":"hi"}}]}']
        )
        adapter = OpenAICompatibleAdapter(PROVIDERS["openrouter"], api_key="k")
        chunks, _, _ = adapter.stream("meta-llama/llama-3.3-70b-instruct:free", "hi")

        assert "".join(chunks) == "hi"
        assert "stream_options" not in captured["json"]

    def test_openai_stream_includes_stream_options(self, monkeypatch) -> None:
        captured = self._patch_stream(
            monkeypatch,
            [
                'data: {"choices":[{"delta":{"content":"hi"}}]}',
                'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}',
            ],
        )
        adapter = build_adapter("openai", _settings(), api_key="k")
        assert adapter is not None
        assert adapter.supports_stream_usage is True
        chunks, usage, _ = adapter.stream("gpt-4o-mini", "hi")

        assert "".join(chunks) == "hi"
        assert captured["json"]["stream_options"] == {"include_usage": True}
        assert usage["input_tokens"] == 1

    def test_direct_construction_defaults_to_no_stream_options(self, monkeypatch) -> None:
        captured = self._patch_stream(
            monkeypatch, ['data: {"choices":[{"delta":{"content":"hi"}}]}']
        )
        adapter = OpenAICompatibleAdapter(PROVIDERS["openai"], api_key="k")
        chunks, _, _ = adapter.stream("gpt-4o-mini", "hi")
        assert "".join(chunks) == "hi"
        assert "stream_options" not in captured["json"]


class TestOpenRouterHeaders:
    def test_attribution_headers_present_without_key(self) -> None:
        adapter = OpenAICompatibleAdapter(PROVIDERS["openrouter"], api_key=None)
        headers = adapter._headers()
        assert "Authorization" not in headers
        assert headers["HTTP-Referer"] == "https://localhost"
        assert headers["X-Title"] == "Bob Agent"

    def test_attribution_headers_present_with_key(self, monkeypatch) -> None:
        post = _patch_client(monkeypatch, {"choices": [{"message": {"content": ""}}]})
        adapter = OpenAICompatibleAdapter(PROVIDERS["openrouter"], api_key="or-key")
        adapter.invoke("meta-llama/llama-3.3-70b-instruct:free", "hi")
        _, kwargs = post.call_args
        headers = kwargs["headers"]
        assert headers["Authorization"] == "Bearer or-key"
        assert headers["HTTP-Referer"] == "https://localhost"
        assert headers["X-Title"] == "Bob Agent"


class TestGeminiStream:
    def _patch_stream(self, monkeypatch, sse_lines: list[str]) -> dict[str, Any]:
        captured: dict[str, Any] = {}
        resp = MagicMock()
        resp.iter_lines.return_value = iter(sse_lines)
        resp.raise_for_status.return_value = None
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False

        def _stream(method: str, url: str, headers: Any = None, json: Any = None) -> Any:
            captured["url"] = url
            captured["json"] = json
            return resp

        client.stream.side_effect = _stream
        monkeypatch.setattr(
            "agent_system.services.providers.httpx.Client",
            MagicMock(return_value=client),
        )
        return captured

    def test_supports_streaming_flag(self) -> None:
        assert GeminiAdapter.supports_streaming is True

    def test_stream_assembles_text_and_function_calls(self, monkeypatch) -> None:
        import json as _json

        evt1 = _json.dumps(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "checking "},
                                {
                                    "functionCall": {
                                        "name": "get_user",
                                        "args": {"id": "u-42"},
                                    }
                                },
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 5},
            }
        )
        evt2 = _json.dumps(
            {"candidates": [{"content": {"parts": [{"text": "done"}]}, "finishReason": "STOP"}]}
        )
        captured = self._patch_stream(monkeypatch, [f"data: {evt1}", f"data: {evt2}"])

        adapter = GeminiAdapter(PROVIDERS["gemini"], api_key="g-key")
        chunks, usage, tool_calls = adapter.stream("gemini-3.6-flash", "hi")
        output = "".join(chunks)

        assert output == "checking done"
        assert usage == {"input_tokens": 3, "output_tokens": 5}
        assert tool_calls == [
            {"id": "call_native_1", "name": "get_user", "arguments": '{"id": "u-42"}'}
        ]
        assert ":streamGenerateContent" in captured["url"]

    def test_finish_reason_normalized(self, monkeypatch) -> None:
        post = _patch_client(
            monkeypatch,
            {"candidates": [{"content": {"parts": []}, "finishReason": "SAFETY"}]},
        )
        adapter = GeminiAdapter(PROVIDERS["gemini"], api_key="g-key")
        result = adapter.invoke("gemini-3.6-flash", "hi")
        assert result["finish_reason"] == "content_filter"
        assert post.called

    def test_finish_reason_max_tokens_maps_to_length(self, monkeypatch) -> None:
        _patch_client(
            monkeypatch,
            {"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]},
        )
        adapter = GeminiAdapter(PROVIDERS["gemini"], api_key="g-key")
        result = adapter.invoke("gemini-3.6-flash", "hi")
        assert result["finish_reason"] == "length"


class TestAnthropic:
    def test_invoke_sends_x_api_key_and_version(self, monkeypatch) -> None:
        post = _patch_client(
            monkeypatch,
            {
                "content": [{"type": "text", "text": "claude reply"}],
                "usage": {"input_tokens": 5, "output_tokens": 10},
            },
        )
        adapter = AnthropicAdapter(PROVIDERS["anthropic"], api_key="sk-ant-test")
        result = adapter.invoke("claude-sonnet-4-5", "test")

        assert result["output"] == "claude reply"
        assert result["usage"]["input_tokens"] == 5
        _, kwargs = post.call_args
        headers = kwargs["headers"]
        assert headers["x-api-key"] == "sk-ant-test"
        assert headers["anthropic-version"] == "2023-06-01"
        assert headers["Accept"] == "application/json"


class TestBuildAdapter:
    def test_build_opens_familiar(self) -> None:
        adapter = build_adapter("groq", _settings(), api_key="gk")
        assert isinstance(adapter, OpenAICompatibleAdapter)

    def test_build_gemini(self) -> None:
        adapter = build_adapter("gemini", _settings(), api_key="gk")
        assert isinstance(adapter, GeminiAdapter)

    def test_build_anthropic(self) -> None:
        adapter = build_adapter("anthropic", _settings(), api_key="ak")
        assert isinstance(adapter, AnthropicAdapter)

    def test_build_unknown_returns_none(self) -> None:
        assert build_adapter("nonexistent", _settings()) is None


class TestPricing:
    def test_free_tier_models_register_zero_cost(self) -> None:
        pricing = build_pricing()
        ids = [m.model_id for m in pricing.all()]
        assert "openai/gpt-oss-20b" in ids  # groq free tier
        assert "auto" not in ids  # tokenrouter "auto" is billable, not free
        for m in pricing.all():
            if m.input_cost_per_1m < 0:
                raise AssertionError(f"negative cost for {m.model_id}")
        assert "gpt-4o-mini" not in ids  # openai not free tier


class TestConfiguredProviders:
    def test_providers_without_keys_shown(self) -> None:
        s = _settings()
        providers = configured_providers(s)
        assert len(providers) >= 12
        ollama = next(p for p in providers if p["key"] == "ollama")
        assert ollama["configured"] is True  # keyless

    def test_providers_with_key_show_configured(self) -> None:
        s = _settings(groq_api_key="test")
        providers = configured_providers(s)
        groq = next(p for p in providers if p["key"] == "groq")
        assert groq["configured"] is True


class TestDefaultModelId:
    def test_explicit_default_model(self) -> None:
        assert default_model_id(_settings(default_model="my-model")) == "my-model"

    def test_from_provider_spec(self) -> None:
        s = _settings(default_provider="groq")
        assert default_model_id(s) == "openai/gpt-oss-20b"


class TestProviderApiKey:
    def test_groq_key(self) -> None:
        s = _settings(groq_api_key="gk")
        assert s.provider_api_key("groq") == "gk"

    def test_unknown_provider(self) -> None:
        assert _settings().provider_api_key("nonexistent") is None

    def test_keyless_provider(self) -> None:
        assert _settings().provider_api_key("ollama") is None


class TestExtraHeaders:
    def test_empty_json(self) -> None:
        assert _settings().extra_headers == {}

    def test_invalid_json(self) -> None:
        assert _settings(provider_extra_headers="not json").extra_headers == {}

    def test_valid_json(self) -> None:
        s = _settings(provider_extra_headers='{"X-API": "abc"}')
        assert s.extra_headers == {"X-API": "abc"}


class TestTestProvider:
    def test_unknown_provider_returns_error(self) -> None:
        result = check_provider(_settings(), "nonexistent")
        assert result["ok"] is False
        assert "unknown provider" in result["error"]


class TestKeyEnvAndBaseEnv:
    def test_env_key_names_consistent_with_config(self) -> None:
        from agent_system.cli.setup import _BASE_ENV, _KEY_ENV

        assert _KEY_ENV["groq"] == "GROQ_API_KEY"
        assert _KEY_ENV["openrouter"] == "OPENROUTER_API_KEY"
        assert _BASE_ENV["gemini"] == "GEMINI_BASE_URL"
        assert _BASE_ENV["tokenrouter"] == "TOKENROUTER_BASE_URL"
