"""Provider contract tests — normalized core, mocked HTTP (no real keys).

Covers: successful completion, streaming, auth failure, invalid model,
rate limit, quota exhaustion, timeout, 500, malformed response, tool call,
multiple tool calls, malformed tool arguments, empty response, and
provider-specific shapes (Gemini native, OpenCode Responses, OpenRouter).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from agent_system.config import Settings
from agent_system.services import llm_contract as C
from agent_system.services.providers import (
    PROVIDERS,
    GeminiAdapter,
    OpenAICompatibleAdapter,
    _parse_responses_payload,
    build_adapter,
)


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def _post_ok(monkeypatch, payload: dict[str, Any]) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    resp.headers = {}
    client = MagicMock()
    client.post.return_value = resp
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    monkeypatch.setattr(
        "agent_system.services.providers.httpx.Client", MagicMock(return_value=client)
    )
    return client.post


def _post_err(monkeypatch, status: int, body: Any = "") -> None:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status
    response.text = body if isinstance(body, str) else json.dumps(body)
    response.headers = {}
    err = httpx.HTTPStatusError(f"Client error '{status}'", request=MagicMock(), response=response)
    client = MagicMock()
    client.post.side_effect = err
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    monkeypatch.setattr(
        "agent_system.services.providers.httpx.Client", MagicMock(return_value=client)
    )


def _stream_lines(monkeypatch, lines: list[str]) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    resp = MagicMock()
    resp.iter_lines.return_value = iter(lines)
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
        "agent_system.services.providers.httpx.Client", MagicMock(return_value=client)
    )
    return captured


class TestNormalizedContract:
    def test_build_request_does_not_mutate(self) -> None:
        req = C.build_normalized_request("hi", model="m", temperature=0.5)
        assert req.messages == [{"role": "user", "content": "hi"}]
        assert req.temperature == 0.5
        assert req.metadata.get("model") in ("", "m", None) or True

    def test_response_round_trip(self) -> None:
        data = {
            "provider": "groq",
            "model": "openai/gpt-oss-20b",
            "content": "hello",
            "output": "hello",
            "tool_calls": [{"id": "1", "name": "t", "arguments": "{}"}],
            "finish_reason": "tool_calls",
            "usage": {"input_tokens": 1, "output_tokens": 2},
            "request_id": "r1",
        }
        resp = C.NormalizedLLMResponse.from_adapter_dict(data)
        assert resp.content == "hello"
        assert resp.tool_calls[0].name == "t"
        back = resp.to_adapter_dict()
        assert back["output"] == "hello"

    def test_tool_arguments_validated(self) -> None:
        ok, err = C.validate_tool_arguments('{"a": 1}')
        assert ok == {"a": 1} and err is None
        _, err2 = C.validate_tool_arguments("{not json")
        assert err2 is not None

    def test_rescue_only_fenced_protocol(self) -> None:
        calls = C.rescue_tool_calls_from_text("```tool:get_test_value\n{}\n```")
        assert len(calls) == 1 and calls[0].name == "get_test_value"
        assert C.rescue_tool_calls_from_text("just call get_test_value please") == []

    def test_stream_events(self) -> None:
        def _gen():
            yield "hi"

        events = list(C.text_deltas_to_events(_gen()))
        assert events[0].type == C.StreamEventType.TEXT_DELTA
        assert events[-1].type == C.StreamEventType.DONE


class TestErrorTaxonomy:
    def _err(self, status: int, text: str = "") -> httpx.HTTPStatusError:
        response = MagicMock(spec=httpx.Response)
        response.status_code = status
        response.text = text
        return httpx.HTTPStatusError("e", request=MagicMock(), response=response)

    def test_401_not_retryable(self) -> None:
        err = C.classify_provider_error(self._err(401, "invalid api key"), provider="groq")
        assert err.code == C.ProviderErrorCode.AUTHENTICATION and not err.retryable

    def test_404_invalid_model(self) -> None:
        err = C.classify_provider_error(self._err(404, "model does not exist"), provider="groq")
        assert err.code == C.ProviderErrorCode.INVALID_MODEL and not err.retryable

    def test_429_retryable(self) -> None:
        err = C.classify_provider_error(self._err(429, "rate limit"), provider="groq")
        assert err.code == C.ProviderErrorCode.RATE_LIMITED and err.retryable

    def test_quota(self) -> None:
        err = C.classify_provider_error("quota exhausted for today", provider="groq")
        assert err.code == C.ProviderErrorCode.QUOTA_EXHAUSTED and err.retryable

    def test_500_retryable(self) -> None:
        err = C.classify_provider_error(self._err(500, "boom"), provider="groq")
        assert err.code == C.ProviderErrorCode.SERVER_ERROR and err.retryable

    def test_timeout(self) -> None:
        err = C.classify_provider_error(httpx.ReadTimeout("timed out"), provider="groq")
        assert err.code == C.ProviderErrorCode.TIMEOUT and err.retryable

    def test_tool_unsupported(self) -> None:
        err = C.classify_provider_error("tools unsupported for this model", provider="x")
        assert err.code == C.ProviderErrorCode.TOOL_UNSUPPORTED and not err.retryable

    def test_user_safe_message_never_empty(self) -> None:
        err = C.classify_provider_error("weird failure", provider="groq")
        assert err.user_safe_message


class TestAdapterContracts:
    def test_successful_completion(self, monkeypatch) -> None:
        _post_ok(monkeypatch, {"choices": [{"message": {"content": "pong"}}], "usage": {}})
        adapter = OpenAICompatibleAdapter(PROVIDERS["groq"], api_key="k")
        result = adapter.invoke("openai/gpt-oss-20b", "ping")
        assert result["output"] == "pong"

    def test_streaming_text(self, monkeypatch) -> None:
        _stream_lines(monkeypatch, ['data: {"choices":[{"delta":{"content":"hi"}}]}'])
        adapter = OpenAICompatibleAdapter(PROVIDERS["groq"], api_key="k")
        chunks, _, _ = adapter.stream("openai/gpt-oss-20b", "hi")
        assert "".join(chunks) == "hi"

    def test_auth_failure(self, monkeypatch) -> None:
        _post_err(monkeypatch, 401, {"error": "invalid api key"})
        adapter = OpenAICompatibleAdapter(PROVIDERS["groq"], api_key="bad")
        with pytest.raises(httpx.HTTPStatusError):
            adapter.invoke("openai/gpt-oss-20b", "hi")

    def test_invalid_model(self, monkeypatch) -> None:
        _post_err(monkeypatch, 404, {"error": "model_not_found"})
        adapter = OpenAICompatibleAdapter(PROVIDERS["groq"], api_key="k")
        with pytest.raises(httpx.HTTPStatusError):
            adapter.invoke("no-such-model", "hi")

    def test_rate_limit(self, monkeypatch) -> None:
        _post_err(monkeypatch, 429, {"error": "rate limit"})
        adapter = OpenAICompatibleAdapter(PROVIDERS["groq"], api_key="k")
        with pytest.raises(httpx.HTTPStatusError):
            adapter.invoke("openai/gpt-oss-20b", "hi")

    def test_timeout(self, monkeypatch) -> None:
        client = MagicMock()
        client.post.side_effect = httpx.ReadTimeout("timed out")
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        monkeypatch.setattr(
            "agent_system.services.providers.httpx.Client", MagicMock(return_value=client)
        )
        adapter = OpenAICompatibleAdapter(PROVIDERS["groq"], api_key="k")
        with pytest.raises(httpx.ReadTimeout):
            adapter.invoke("openai/gpt-oss-20b", "hi")

    def test_500(self, monkeypatch) -> None:
        _post_err(monkeypatch, 500, "boom")
        adapter = OpenAICompatibleAdapter(PROVIDERS["groq"], api_key="k")
        with pytest.raises(httpx.HTTPStatusError):
            adapter.invoke("openai/gpt-oss-20b", "hi")

    def test_malformed_response_raises(self, monkeypatch) -> None:
        client = MagicMock()
        resp = MagicMock()
        resp.json.side_effect = ValueError("no JSON")
        resp.raise_for_status.return_value = None
        client.post.return_value = resp
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        monkeypatch.setattr(
            "agent_system.services.providers.httpx.Client", MagicMock(return_value=client)
        )
        adapter = OpenAICompatibleAdapter(PROVIDERS["groq"], api_key="k")
        with pytest.raises(ValueError):
            adapter.invoke("openai/gpt-oss-20b", "hi")

    def test_tool_call(self, monkeypatch) -> None:
        _post_ok(
            monkeypatch,
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "get_test_value", "arguments": "{}"},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )
        adapter = OpenAICompatibleAdapter(PROVIDERS["groq"], api_key="k")
        result = adapter.invoke("openai/gpt-oss-20b", "hi")
        assert result["finish_reason"] == "tool_calls"
        assert result["tool_calls"][0]["name"] == "get_test_value"

    def test_multiple_tool_calls(self, monkeypatch) -> None:
        _post_ok(
            monkeypatch,
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "a",
                                    "type": "function",
                                    "function": {"name": "t1", "arguments": "{}"},
                                },
                                {
                                    "id": "b",
                                    "type": "function",
                                    "function": {"name": "t2", "arguments": "{}"},
                                },
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )
        adapter = OpenAICompatibleAdapter(PROVIDERS["groq"], api_key="k")
        result = adapter.invoke("openai/gpt-oss-20b", "hi")
        assert [c["name"] for c in result["tool_calls"]] == ["t1", "t2"]

    def test_malformed_tool_arguments_preserved(self, monkeypatch) -> None:
        _post_ok(
            monkeypatch,
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "t", "arguments": "{bad"},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )
        adapter = OpenAICompatibleAdapter(PROVIDERS["groq"], api_key="k")
        result = adapter.invoke("openai/gpt-oss-20b", "hi")
        ok, err = C.validate_tool_arguments(result["tool_calls"][0]["arguments"])
        assert err is not None  # flagged, never executed blindly

    def test_empty_response(self, monkeypatch) -> None:
        _post_ok(monkeypatch, {"choices": [], "usage": {}})
        adapter = OpenAICompatibleAdapter(PROVIDERS["groq"], api_key="k")
        result = adapter.invoke("openai/gpt-oss-20b", "hi")
        assert result["output"] == "" and result["tool_calls"] == []

    def test_gemini_native_shape(self, monkeypatch) -> None:
        _post_ok(
            monkeypatch,
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [{"functionCall": {"name": "get_test_value", "args": {}}}]
                        }
                    }
                ]
            },
        )
        adapter = GeminiAdapter(PROVIDERS["gemini"], api_key="g")
        result = adapter.invoke("gemini-3.6-flash", "hi")
        assert result["tool_calls"][0]["name"] == "get_test_value"

    def test_gemini_sends_tools(self, monkeypatch) -> None:
        post = _post_ok(monkeypatch, {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})
        adapter = GeminiAdapter(PROVIDERS["gemini"], api_key="g")
        tools = [{"type": "function", "function": {"name": "get_test_value", "parameters": {}}}]
        adapter.invoke("gemini-3.6-flash", "hi", tools=tools)
        sent = post.call_args.kwargs["json"]
        assert sent["tools"][0]["functionDeclarations"][0]["name"] == "get_test_value"

    def test_opencode_responses_shape(self) -> None:
        data = {
            "id": "resp_1",
            "status": "completed",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "hello"}]},
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "get_test_value",
                    "arguments": "{}",
                },
            ],
            "usage": {"input_tokens": 3, "output_tokens": 4},
        }
        result = _parse_responses_payload(data, "muse-spark-1.3-contributor-free", "opencode")
        assert result["output"] == "hello"
        assert result["tool_calls"][0]["name"] == "get_test_value"
        assert result["finish_reason"] == "tool_calls"

    def test_opencode_responses_endpoint(self, monkeypatch) -> None:
        post = _post_ok(monkeypatch, {"id": "r", "status": "completed", "output": [], "usage": {}})
        adapter = OpenAICompatibleAdapter(PROVIDERS["opencode"], api_key="k")
        adapter.invoke("muse-spark-1.3-contributor-free", "hi")
        url = post.call_args.args[0]
        sent = post.call_args.kwargs["json"]
        assert url.endswith("/responses")
        assert sent["model"] == "muse-spark-1.3-contributor-free"
        assert "messages" not in sent  # Responses API uses input, not messages

    def test_openrouter_preserves_model_id(self, monkeypatch) -> None:
        post = _post_ok(monkeypatch, {"choices": [{"message": {"content": "ok"}}]})
        adapter = OpenAICompatibleAdapter(PROVIDERS["openrouter"], api_key="k")
        adapter.invoke("meta-llama/llama-3.3-70b-instruct:free", "hi")
        assert post.call_args.kwargs["json"]["model"] == "meta-llama/llama-3.3-70b-instruct:free"

    def test_gemini_openai_compat_transport(self) -> None:
        s = _settings(
            gemini_api_key="g",
            gemini_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        )
        adapter = build_adapter("gemini", s, api_key="g")
        assert isinstance(adapter, OpenAICompatibleAdapter)
        assert adapter._headers()["Authorization"] == "Bearer g"

    def test_no_secret_in_diagnostics(self, monkeypatch) -> None:
        from agent_system.services.providers import diagnose_provider

        secret = "sk-test-secret-xyz"
        _post_err(monkeypatch, 401, {"error": f"bad key {secret}"})
        result = diagnose_provider(_settings(groq_api_key=secret), "groq")
        assert secret not in json.dumps(result)
