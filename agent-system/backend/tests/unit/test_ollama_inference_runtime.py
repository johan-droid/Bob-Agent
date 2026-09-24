"""Ollama Cloud-first inference runtime: mocked provider lifecycle tests.

Every test here runs with mocked adapters — no paid API and no real provider
quota is required. The full failure/recovery lifecycle is covered:

- Ollama Cloud: success, streaming, tool calls, malformed response, timeout,
  unavailable model;
- session locking: selected once, stable across repeated requests;
- retry: transient failure, 429, exponential backoff, exhaustion;
- emergency fallback: outage, checkpoint, compatible selection, context
  preservation, resume;
- capability routing: tool-required / coding / long-context;
- recovery: Ollama comes back, fallback is never sticky;
- Telegram: truthful progress, no duplicate notices, no internal ids.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent_system.config import Settings
from agent_system.domain.events import Event
from agent_system.infra.db import make_engine, make_session_factory, session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Base, EventRow, InferenceModelLock, ModelCall
from agent_system.services import inference_runtime as rt
from agent_system.services.checkpoints import (
    build_checkpoint_payload,
    load_checkpoint,
    mark_checkpoint_resumed,
    save_checkpoint,
)
from agent_system.services.inference_runtime import (
    ErrorKind,
    HealthState,
    InferenceHealthTracker,
    ModelRole,
    ModelSelection,
    SessionModelLock,
    TaskFamily,
    classify_task,
    describe_selection,
    effective_primary,
    is_retryable,
    normalize_error,
    resolve_role_models,
    retry_delays,
    select_model,
    validate_configuration,
)
from agent_system.services.llm_catalog import (
    DEFAULT_CATALOG,
    CapabilityCatalog,
    ModelCapability,
)
from agent_system.services.model_router import ModelRouter, ProviderAdapter

#: The Ollama Cloud ids Bob's catalog declares (never invented in tests).
OLLAMA_MODELS = ("gpt-oss:20b", "gpt-oss:120b", "gemma4:31b", "nemotron-3-ultra")


@pytest.fixture(autouse=True)
def _clean_runtime_state() -> Any:
    """Isolate the process-wide lock + health registries between tests."""
    rt.INFERENCE_LOCKS.clear_all()
    rt.INFERENCE_HEALTH.reset()
    yield
    rt.INFERENCE_LOCKS.clear_all()
    rt.INFERENCE_HEALTH.reset()


@pytest.fixture()
def factory(tmp_path: Path) -> Any:
    engine = make_engine(f"sqlite:///{tmp_path / 'inference.db'}")
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


def _settings(**overrides: Any) -> Settings:
    """Ollama Cloud-first settings with every emergency key configured."""
    base: dict[str, Any] = {
        "primary_provider": "ollama_cloud",
        "default_provider": "ollama_cloud",
        "ollama_cloud_api_key": "test-ollama-key",
        "groq_api_key": "test-groq-key",
        "gemini_api_key": "test-gemini-key",
        "openrouter_api_key": "test-openrouter-key",
        # Chat -> FAST role, so a "hello" request resolves to gpt-oss:120b.
        "ollama_fast_model": "gpt-oss:120b",
        "ollama_general_model": "gpt-oss:120b",
        "ollama_coding_model": "nemotron-3-ultra",
        "ollama_reasoning_model": "gpt-oss:120b",
        "ollama_long_context_model": "gemma4:31b",
        "ollama_tool_model": "nemotron-3-ultra",
        # Streaming is exercised by its own test; everywhere else a single
        # deterministic (non-streamed) completion keeps assertions exact.
        "ollama_streaming": False,
        "ollama_retry_attempts": 3,
        "ollama_retry_base_delay": 1.0,
        "ollama_retry_max_delay": 20.0,
        "emergency_fallback_enabled": True,
        "fallback_providers": "groq,gemini,openrouter",
        "task_checkpointing": True,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


class ScriptedAdapter(ProviderAdapter):
    """Deterministic adapter driven by a per-model list of outcomes.

    An outcome is a result dict (``output``/``usage``/``tool_calls``) or
    ``{"error": "..."}`` meaning the adapter call raises. The last outcome
    repeats, so a single-entry script always yields the same result.
    """

    supports_streaming = True

    def __init__(
        self,
        scripts: dict[str, list[dict[str, Any]]] | None = None,
        default_outcome: dict[str, Any] | None = None,
    ) -> None:
        self.scripts = {k: list(v) for k, v in (scripts or {}).items()}
        self.default_outcome = default_outcome
        self.calls: list[str] = []

    def _outcome(self, model_id: str) -> dict[str, Any]:
        self.calls.append(model_id)
        script = self.scripts.get(model_id)
        if not script:
            if self.default_outcome is not None:
                outcome = dict(self.default_outcome)
                if "error" in outcome:
                    raise _AdapterError(str(outcome["error"]))
                return outcome
            return {"output": "default answer", "usage": {"input_tokens": 1, "output_tokens": 1}}
        outcome = script.pop(0) if len(script) > 1 else script[0]
        if "error" in outcome:
            raise _AdapterError(str(outcome["error"]))
        return dict(outcome)

    def invoke(self, model_id: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        return self._outcome(model_id)

    def stream(self, model_id: str, prompt: str, **kwargs: Any) -> Any:
        outcome = self._outcome(model_id)
        text = str(outcome.get("output") or "")

        def chunks() -> Any:
            for word in text.split(" "):
                yield word + " "

        return chunks(), dict(outcome.get("usage") or {}), outcome.get("tool_calls")


class _AdapterError(RuntimeError):
    """Adapter-level failure carrying the raw provider text."""


class _FakeResponse:
    """Minimal stand-in for ``httpx.Response`` (status, body, headers)."""

    def __init__(self, status_code: int, text: str, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class _HttpStatusError(_AdapterError):
    """Adapter failure shaped like ``httpx.HTTPStatusError``."""

    def __init__(self, status_code: int, reason: str, body: str = "") -> None:
        super().__init__(
            f"Client error '{status_code} {reason}' for url 'https://x/v1/chat/completions'"
        )
        self.response = _FakeResponse(status_code, body)


def _router(adapters: dict[str, Any], settings: Settings) -> ModelRouter:
    router = ModelRouter(EventBus(), settings=settings)
    index: dict[str, str] = {model: "ollama_cloud" for model in OLLAMA_MODELS}
    index["openai/gpt-oss-20b"] = "groq"
    index["gemini-3.6-flash"] = "gemini"
    index["meta-llama/llama-3.3-70b-instruct:free"] = "openrouter"
    index["deepseek/deepseek-chat-v3-0324:free"] = "openrouter"
    router.register_provider_index(index)
    for provider, adapter in adapters.items():
        router.register_adapter(provider, adapter)
    return router


SESSION = "ses_ollama000000000001"
TASK = "task_ollama00000000001"


def _invoke(
    router: ModelRouter,
    factory: Any,
    settings: Settings,
    text: str = "hello",
    **kwargs: Any,
) -> Any:
    """Runtime call with a deterministic clock (no real sleeping)."""
    kwargs.setdefault("session_id", SESSION)
    kwargs.setdefault("task_id", TASK)
    kwargs.setdefault("sleeper", lambda _seconds: None)
    return rt.invoke(router, factory, settings, "PROMPT", text=text, **kwargs)


# ---------------------------------------------------------------------------
# Ollama Cloud core paths
# ---------------------------------------------------------------------------


def test_ollama_successful_request(factory: Any) -> None:
    settings = _settings(ollama_coding_model="gpt-oss:120b")
    adapter = ScriptedAdapter({"gpt-oss:120b": [{"output": "done", "usage": {}}]})
    router = _router({"ollama_cloud": adapter}, settings)

    invocation = _invoke(router, factory, settings, text="fix the bug in my repo")

    assert invocation.ok
    assert invocation.output == "done"
    assert invocation.selection.provider == "ollama_cloud"
    assert invocation.selection.model_id == "gpt-oss:120b"
    assert invocation.fallback_used is False
    assert invocation.notice is None
    with session_scope(factory) as db:
        rows = db.query(ModelCall).all()
    assert rows and rows[0].provider == "ollama_cloud"


def test_ollama_streaming_emits_tokens(factory: Any) -> None:
    settings = _settings(ollama_streaming=True)
    adapter = ScriptedAdapter({"gpt-oss:120b": [{"output": "one two", "usage": {}}]})
    router = _router({"ollama_cloud": adapter}, settings)
    tokens: list[str] = []

    invocation = _invoke(router, factory, settings, text="hello", on_token=tokens.append)

    assert invocation.ok
    assert invocation.selection.model_id == "gpt-oss:120b"
    assert tokens, "a streaming adapter must yield token deltas"
    assert "".join(tokens).strip() == "one two"


def test_catalog_declares_only_free_plan_ollama_models() -> None:
    """Every catalogued Ollama Cloud id is usable on the free plan.

    Models outside the account's plan answer HTTP 402 "not included in your
    free usage"; registering one means selection hands the runtime a model
    it can never use. This pins the verified free-plan set (2026-09-24).
    """
    ollama_ids = {m.model_id for m in DEFAULT_CATALOG.all() if m.provider == "ollama_cloud"}
    assert ollama_ids == {
        "gpt-oss:20b",
        "gpt-oss:120b",
        "gemma4:31b",
        "nemotron-3-nano:30b",
        "nemotron-3-super",
        "nemotron-3-ultra",
    }
    # Deliberately excluded paid/retired ids must never come back.
    excluded_paid = {"qwen3.5:397b", "deepseek-v4.1-flash", "qwen2.5-coder", "llama3.3"}
    assert ollama_ids.isdisjoint(excluded_paid)
    # The provider spec offers the same verified set.
    from agent_system.services.providers import provider_spec

    spec = provider_spec("ollama_cloud")
    assert spec is not None and set(spec.models) == ollama_ids


def test_ollama_tool_calls_are_carried_through(factory: Any) -> None:
    settings = _settings()
    tool_call = {"id": "call_1", "name": "file_read", "arguments": '{"path": "a.py"}'}
    adapter = ScriptedAdapter(
        {
            "nemotron-3-ultra": [
                {"output": "reading the file", "usage": {}, "tool_calls": [tool_call]}
            ]
        }
    )
    router = _router({"ollama_cloud": adapter}, settings)

    # TOOL_HEAVY task -> TOOL_USE role -> the configured tool model.
    invocation = _invoke(router, factory, settings, text="create a task and run the script")

    assert invocation.ok
    assert invocation.selection.role == ModelRole.TOOL_USE
    assert invocation.selection.model_id == "nemotron-3-ultra"
    assert invocation.result.tool_calls == [tool_call]


def test_ollama_malformed_response_is_retried(factory: Any) -> None:
    """An empty completion is a failure: the SAME model is retried."""
    settings = _settings()
    adapter = ScriptedAdapter(
        {"gpt-oss:120b": [{"output": ""}, {"output": ""}, {"output": "recovered"}]}
    )
    router = _router({"ollama_cloud": adapter}, settings)

    invocation = _invoke(router, factory, settings)

    assert invocation.ok
    assert invocation.output == "recovered"
    assert adapter.calls == ["gpt-oss:120b"] * 3
    # Two failed attempts recorded, then the success — the SAME model each time.
    assert [attempt["ok"] for attempt in invocation.attempts] == [False, False, True]
    assert all(a["model_id"] == "gpt-oss:120b" for a in invocation.attempts)


def test_ollama_timeout_is_retried_on_same_model(factory: Any) -> None:
    settings = _settings()
    adapter = ScriptedAdapter(
        {"gpt-oss:120b": [{"error": "request timed out"}, {"output": "ok after retry"}]}
    )
    router = _router({"ollama_cloud": adapter}, settings)

    invocation = _invoke(router, factory, settings)

    assert invocation.ok
    assert invocation.error_kind is None
    assert adapter.calls == ["gpt-oss:120b", "gpt-oss:120b"]


def test_ollama_unavailable_model_switches_to_compatible_ollama_model(factory: Any) -> None:
    """MODEL_NOT_FOUND is never retried — a compatible Ollama model is used."""
    settings = _settings()
    adapter = ScriptedAdapter(
        {
            "gpt-oss:120b": [{"error": "404 model 'gpt-oss:120b' not found"}],
            "gpt-oss:20b": [{"output": "served by alternate"}],
        }
    )
    router = _router({"ollama_cloud": adapter}, settings)

    invocation = _invoke(router, factory, settings)

    assert invocation.ok
    assert invocation.selection.provider == "ollama_cloud"
    assert invocation.selection.model_id == "gpt-oss:20b"
    # Never left Ollama Cloud: no emergency notice.
    assert invocation.notice is None
    assert invocation.fallback_used is False
    assert adapter.calls == ["gpt-oss:120b", "gpt-oss:20b"]


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("401 invalid api key", ErrorKind.AUTH_FAILED),
        ("404 unknown model", ErrorKind.MODEL_NOT_FOUND),
        ("429 too many requests", ErrorKind.RATE_LIMITED),
        ("request timed out", ErrorKind.TIMEOUT),
        ("maximum context length exceeded", ErrorKind.CONTEXT_TOO_LARGE),
        ("tool calling unsupported by this model", ErrorKind.TOOL_UNSUPPORTED),
        ("no adapter registered for provider 'ollama_cloud'", ErrorKind.PROVIDER_UNAVAILABLE),
        ("503 service unavailable", ErrorKind.PROVIDER_UNAVAILABLE),
        ("500 internal server error", ErrorKind.SERVER_ERROR),
        ("400 bad request", ErrorKind.INVALID_REQUEST),
        # A plan/access refusal is NOT a rate limit: it must never be retried.
        (
            "HTTPStatusError: Client error '402 Payment Required' for url "
            "'https://ollama.com/v1/chat/completions'",
            ErrorKind.QUOTA_EXHAUSTED,
        ),
        ("insufficient_quota: you exceeded your current quota", ErrorKind.QUOTA_EXHAUSTED),
        ("this model requires a subscription to use", ErrorKind.QUOTA_EXHAUSTED),
    ],
)
def test_normalize_error_taxonomy(text: str, expected: ErrorKind) -> None:
    assert normalize_error(text) == expected


def test_only_recoverable_errors_are_retried() -> None:
    assert is_retryable(ErrorKind.TIMEOUT) is True
    assert is_retryable(ErrorKind.RATE_LIMITED) is True
    assert is_retryable(ErrorKind.SERVER_ERROR) is True
    for kind in (
        ErrorKind.AUTH_FAILED,
        ErrorKind.MODEL_NOT_FOUND,
        ErrorKind.CONTEXT_TOO_LARGE,
        ErrorKind.TOOL_UNSUPPORTED,
        ErrorKind.INVALID_REQUEST,
        ErrorKind.QUOTA_EXHAUSTED,
    ):
        assert is_retryable(kind) is False


# ---------------------------------------------------------------------------
# Session model lock
# ---------------------------------------------------------------------------


def test_session_lock_keeps_one_model_across_requests() -> None:
    settings = _settings(ollama_coding_model="gpt-oss:120b")
    locks = SessionModelLock()

    first = select_model(settings, "fix the bug in my repo", locks=locks, session_id="ses_A")
    second = select_model(settings, "fix another bug in my repo", locks=locks, session_id="ses_A")

    assert first.model_id == second.model_id == "gpt-oss:120b"
    assert second.locked is True
    assert second.reason.startswith("session_lock:")


def test_session_lock_survives_a_reconfigured_role_model() -> None:
    """Changing the configured model mid-session must NOT switch the model."""
    locks = SessionModelLock()
    select_model(
        _settings(ollama_coding_model="gpt-oss:120b"),
        "fix the bug in my repo",
        locks=locks,
        session_id="ses_A",
    )

    reconfigured = _settings(ollama_coding_model="nemotron-3-ultra")
    same_session = select_model(reconfigured, "refactor this repo", locks=locks, session_id="ses_A")
    new_session = select_model(reconfigured, "refactor this repo", locks=locks, session_id="ses_B")

    assert same_session.model_id == "gpt-oss:120b"  # locked, not re-rolled
    assert same_session.locked is True
    assert new_session.model_id == "nemotron-3-ultra"  # fresh session re-selects


def test_session_lock_released_when_model_becomes_unavailable() -> None:
    locks = SessionModelLock()
    health = InferenceHealthTracker()
    settings = _settings(ollama_coding_model="gpt-oss:120b")
    select_model(settings, "fix the bug in my repo", locks=locks, health=health, session_id="ses_A")

    health.report_failure("ollama_cloud", "gpt-oss:120b", ErrorKind.AUTH_FAILED, "401 bad key")

    # The stale lock is dropped and the (now different) role model is chosen
    # fresh — never re-selected through the dead lock.
    reselected = select_model(
        _settings(ollama_coding_model="nemotron-3-ultra"),
        "fix the bug in my repo",
        locks=locks,
        health=health,
        session_id="ses_A",
    )
    assert reselected.model_id == "nemotron-3-ultra"
    assert reselected.locked is False
    assert not reselected.reason.startswith("session_lock:")


# ---------------------------------------------------------------------------
# Retry / backoff
# ---------------------------------------------------------------------------


def test_retry_delays_are_exponential_and_capped() -> None:
    settings = _settings(ollama_retry_base_delay=1.0, ollama_retry_max_delay=4.0)
    assert retry_delays(settings, 3) == [1.0, 2.0]  # 3 tries -> 2 waits
    assert retry_delays(settings, 5) == [1.0, 2.0, 4.0, 4.0]  # capped, never unbounded


def test_rate_limited_backoff_is_four_times_longer() -> None:
    settings = _settings(ollama_retry_base_delay=1.0, ollama_retry_max_delay=100.0)
    assert retry_delays(settings, 3, first_kind=ErrorKind.RATE_LIMITED) == [4.0, 8.0]


def test_rate_limited_retry_respects_provider_retry_after() -> None:
    from agent_system.services.provider_health import GLOBAL_HEALTH_TRACKER

    settings = _settings(ollama_retry_base_delay=1.0, ollama_retry_max_delay=20.0)
    GLOBAL_HEALTH_TRACKER.update_rate_limits("ollama_cloud", "gpt-oss:120b", {"retry-after": "30"})
    try:
        delay = rt._delay_for(  # noqa: SLF001 - the backoff rule IS the behaviour
            settings, ErrorKind.RATE_LIMITED, 1, "ollama_cloud", "gpt-oss:120b"
        )
    finally:
        GLOBAL_HEALTH_TRACKER.report_success("ollama_cloud", "gpt-oss:120b")
    # Honours Retry-After but never exceeds the configured cap.
    assert delay == pytest.approx(20.0)


def test_retry_exhaustion_stops_without_fallback_when_unconfigured(factory: Any) -> None:
    settings = _settings(groq_api_key=None, gemini_api_key=None, openrouter_api_key=None)
    adapter = ScriptedAdapter({"gpt-oss:120b": [{"error": "503 service unavailable"}]})
    router = _router({"ollama_cloud": adapter}, settings)

    invocation = _invoke(router, factory, settings)

    assert invocation.ok is False
    assert invocation.fallback_used is False
    assert invocation.error_kind == ErrorKind.PROVIDER_UNAVAILABLE
    # Bounded: exactly ollama_retry_attempts tries, never a busy loop.
    assert len(adapter.calls) == settings.ollama_retry_attempts


def test_transient_failure_then_success_uses_bounded_backoff(factory: Any) -> None:
    settings = _settings(ollama_retry_base_delay=2.0)
    adapter = ScriptedAdapter(
        {"gpt-oss:120b": [{"error": "connection reset"}, {"output": "fine now"}]}
    )
    router = _router({"ollama_cloud": adapter}, settings)
    sleeps: list[float] = []

    invocation = _invoke(router, factory, settings, sleeper=sleeps.append)

    assert invocation.ok
    assert sleeps == [2.0]  # exactly one bounded wait, no busy loop


# ---------------------------------------------------------------------------
# Emergency fallback + checkpointing
# ---------------------------------------------------------------------------


def test_ollama_outage_falls_back_with_checkpoint_and_notice(factory: Any) -> None:
    settings = _settings(ollama_retry_attempts=2)
    adapter = ScriptedAdapter({"gpt-oss:120b": [{"error": "connection refused"}]})
    groq = ScriptedAdapter({"openai/gpt-oss-20b": [{"output": "fallback answer"}]})
    router = _router({"ollama_cloud": adapter, "groq": groq}, settings)
    events: list[tuple[str, dict[str, Any]]] = []

    invocation = _invoke(
        router,
        factory,
        settings,
        emit=lambda event_type, payload: events.append((event_type, payload)),
        checkpoint_payload=build_checkpoint_payload(
            task_state="running",
            context_ref={"session_id": SESSION, "task_id": TASK},
            completed_steps=["loaded context"],
            pending_step="answer the user",
        ),
    )

    assert invocation.ok
    assert invocation.fallback_used is True
    assert invocation.selection.provider == "groq"
    assert invocation.selection.model_id == "openai/gpt-oss-20b"
    assert invocation.notice == rt.FALLBACK_NOTICE
    assert invocation.checkpoint_id is not None

    activated = [p for t, p in events if t == "inference.fallback_activated"]
    assert len(activated) == 1
    assert activated[0]["from_provider"] == "ollama_cloud"
    assert activated[0]["to_provider"] == "groq"
    assert activated[0]["error_kind"] == ErrorKind.PROVIDER_UNAVAILABLE.value

    checkpoint = load_checkpoint(factory, TASK)
    assert checkpoint is not None
    # Context is preserved verbatim so the task can resume where it stopped.
    assert checkpoint["payload"]["pending_step"] == "answer the user"
    assert checkpoint["payload"]["completed_steps"] == ["loaded context"]
    assert checkpoint["payload"]["context_ref"] == {"session_id": SESSION, "task_id": TASK}
    assert checkpoint["payload"]["active_provider"] == "ollama_cloud"
    assert checkpoint["error_kind"] == ErrorKind.PROVIDER_UNAVAILABLE.value
    # Resumed on the fallback provider, not left dangling.
    assert checkpoint["status"] == "resumed"
    assert checkpoint["provider"] == "groq"


def test_only_compatible_emergency_providers_are_selected(factory: Any) -> None:
    """A tool-required task must not fall back to a tool-less model."""
    settings = _settings(ollama_retry_attempts=1)
    adapter = ScriptedAdapter({"nemotron-3-ultra": [{"error": "connection refused"}]})
    groq = ScriptedAdapter({"openai/gpt-oss-20b": [{"output": "tool-capable fallback"}]})
    router = _router({"ollama_cloud": adapter, "groq": groq}, settings)

    invocation = _invoke(router, factory, settings, text="run the tests in my repo")

    assert invocation.ok
    for provider, model in invocation.selection.candidates[:1]:
        assert provider == "groq"
        assert model == "openai/gpt-oss-20b"


def test_checkpoint_never_stores_secrets(factory: Any) -> None:
    payload = build_checkpoint_payload(
        active_provider="ollama_cloud",
        active_model="gpt-oss:120b",
        execution={"api_key": "sk-super-secret", "authorization": "Bearer abc123"},
    )
    cid = save_checkpoint(
        factory,
        task_id="task_secret000000000001",
        session_id="ses_secret0000000000001",
        provider="ollama_cloud",
        model_id="gpt-oss:120b",
        payload=payload,
    )
    assert cid is not None
    stored = load_checkpoint(factory, "task_secret000000000001")
    assert stored is not None
    blob = json.dumps(stored["payload"])
    assert "sk-super-secret" not in blob
    assert "abc123" not in blob


def test_checkpoint_save_is_idempotent(factory: Any) -> None:
    first = save_checkpoint(
        factory,
        task_id="task_idem0000000000001",
        provider="ollama_cloud",
        model_id="gpt-oss:120b",
        payload=build_checkpoint_payload(pending_step="step one"),
    )
    second = save_checkpoint(
        factory,
        task_id="task_idem0000000000001",
        provider="groq",
        model_id="openai/gpt-oss-20b",
        error_kind="TIMEOUT",
        payload=build_checkpoint_payload(pending_step="step two"),
    )
    assert first == second  # one row, upserted
    stored = load_checkpoint(factory, "task_idem0000000000001")
    assert stored is not None
    assert stored["payload"]["pending_step"] == "step two"
    assert stored["provider"] == "groq"

    assert mark_checkpoint_resumed(factory, "task_idem0000000000001", provider="groq") is True
    assert load_checkpoint(factory, "task_idem0000000000001")["status"] == "resumed"


def test_fallback_is_task_scoped_and_never_sticky(factory: Any) -> None:
    """A fallback in one task must not change the next task's provider."""
    settings = _settings(ollama_retry_attempts=1)
    ollama = ScriptedAdapter({"gpt-oss:120b": [{"error": "connection refused"}]})
    groq = ScriptedAdapter({"openai/gpt-oss-20b": [{"output": "fallback"}]})

    first = _invoke(
        _router({"ollama_cloud": ollama, "groq": groq}, settings),
        factory,
        settings,
        session_id="ses_taskA00000000000001",
        task_id="task_A00000000000000001",
    )
    assert first.fallback_used is True

    # Ollama Cloud recovers, and a NEW task re-evaluates it (not sticky).
    recovered = ScriptedAdapter({"gpt-oss:120b": [{"output": "ollama is back"}]})
    second = _invoke(
        _router({"ollama_cloud": recovered, "groq": groq}, settings),
        factory,
        settings,
        session_id="ses_taskB00000000000002",
        task_id="task_B00000000000000002",
    )

    assert second.ok
    assert second.selection.provider == "ollama_cloud"
    assert second.fallback_used is False
    assert rt.INFERENCE_HEALTH.state_for("ollama_cloud", "gpt-oss:120b") == HealthState.HEALTHY


def test_ollama_available_never_touches_emergency_providers(factory: Any) -> None:
    settings = _settings()
    ollama = ScriptedAdapter({"gpt-oss:120b": [{"output": "primary"}]})
    groq = ScriptedAdapter({"openai/gpt-oss-20b": [{"output": "should not run"}]})
    router = _router({"ollama_cloud": ollama, "groq": groq}, settings)

    invocation = _invoke(router, factory, settings)

    assert invocation.ok
    assert invocation.selection.provider == "ollama_cloud"
    assert groq.calls == []


# ---------------------------------------------------------------------------
# Plan / access refusals (HTTP 402 on a model outside the account plan)
# ---------------------------------------------------------------------------


class _PlanRefusingAdapter(ProviderAdapter):
    """Refuses a model list with a real-shaped 402, serves the rest."""

    supports_streaming = False

    def __init__(self, blocked: set[str], paid_body: str = "") -> None:
        self.blocked = blocked
        self.paid_body = paid_body
        self.calls: list[str] = []

    def invoke(self, model_id: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(model_id)
        if model_id in self.blocked:
            raise _HttpStatusError(402, "Payment Required", self.paid_body)
        return {"output": f"served by {model_id}", "usage": {}}

    def stream(self, model_id: str, prompt: str, **kwargs: Any) -> Any:  # pragma: no cover
        return iter([self.invoke(model_id, prompt, **kwargs)["output"]]), {}, None


def test_402_is_not_retried_and_switches_to_another_ollama_model(factory: Any) -> None:
    """A model outside the plan is dropped after ONE call, not retried."""
    settings = _settings(ollama_retry_attempts=3)
    adapter = _PlanRefusingAdapter(blocked={"gpt-oss:120b"})
    router = _router({"ollama_cloud": adapter}, settings)

    invocation = _invoke(router, factory, settings)

    assert invocation.ok
    # Exactly one call on the refused model — no bounded retry burned on it.
    assert adapter.calls.count("gpt-oss:120b") == 1
    assert invocation.selection.provider == "ollama_cloud"
    assert invocation.selection.model_id != "gpt-oss:120b"
    # Still Ollama Cloud: never left for an external provider.
    assert invocation.fallback_used is False
    assert invocation.notice is None
    assert [a["error_kind"] for a in invocation.attempts if not a["ok"]] == [
        ErrorKind.QUOTA_EXHAUSTED.value
    ]


def test_402_model_is_skipped_by_later_selections(factory: Any) -> None:
    """The refusal is learned, so the next task does not repeat the call."""
    settings = _settings()
    adapter = _PlanRefusingAdapter(blocked={"gpt-oss:120b"})
    router = _router({"ollama_cloud": adapter}, settings)

    first = _invoke(router, factory, settings, session_id="ses_paid00000000001")
    assert first.ok

    assert rt.INFERENCE_HEALTH.state_for("ollama_cloud", "gpt-oss:120b") == HealthState.UNAVAILABLE
    # A brand-new session re-selects without re-calling the refused model.
    second = select_model(settings, "hello", session_id="ses_paid00000000002")
    assert second.model_id != "gpt-oss:120b"

    calls_before = list(adapter.calls)
    _invoke(router, factory, settings, session_id="ses_paid00000000003")
    assert "gpt-oss:120b" not in adapter.calls[len(calls_before) :]


def test_provider_error_body_is_captured_and_redacted(factory: Any) -> None:
    """A bare '400 Bad Request' becomes diagnosable — and never leaks a secret."""
    settings = _settings(ollama_retry_attempts=1)
    body = json.dumps(
        {
            "error": {
                "message": "model `gpt-oss:120b` does not exist",
                "api_key": "sk-leaked-secret-value",
            }
        }
    )
    adapter = _PlanRefusingAdapter(blocked={"gpt-oss:120b"}, paid_body=body)
    router = _router({"ollama_cloud": adapter}, settings)

    _invoke(router, factory, settings)

    with session_scope(factory) as db:
        rows = db.query(ModelCall).all()
    messages = [str((row.error_json or {}).get("error") or "") for row in rows]
    joined = " ".join(messages)
    assert "provider_body" in joined
    assert "does not exist" in joined  # the provider's actual reason
    assert "sk-leaked-secret-value" not in joined  # redacted


def test_emergency_400_is_not_retried_and_moves_on(factory: Any) -> None:
    """A rejected emergency model must not consume the whole retry budget."""
    settings = _settings(ollama_retry_attempts=1)
    # Every Ollama Cloud model is out of plan: the emergency layer is reached.
    ollama = ScriptedAdapter(default_outcome={"error": "402 Payment Required"})
    groq = ScriptedAdapter(default_outcome={"error": "400 Bad Request"})
    router = _router({"ollama_cloud": ollama, "groq": groq}, settings)

    invocation = _invoke(router, factory, settings)

    # groq's single catalogue model was tried exactly once (no retry storm).
    assert len(groq.calls) == 1
    assert invocation.fallback_used is True
    assert invocation.notice == rt.FALLBACK_NOTICE
    kinds = [a.get("error_kind") for a in invocation.attempts if not a["ok"]]
    assert ErrorKind.INVALID_REQUEST.value in kinds


# ---------------------------------------------------------------------------
# Capability routing
# ---------------------------------------------------------------------------


def test_tool_required_task_never_selects_a_non_tool_model() -> None:
    selection = select_model(_settings(), "run the tests in my repo", requires_tools=True)

    cap = DEFAULT_CATALOG.get(selection.provider, selection.model_id)
    assert cap is not None
    assert cap.effective_supports_tools is True
    for provider, model in selection.candidates:
        candidate = DEFAULT_CATALOG.get(provider, model)
        if candidate is not None and candidate.provider == "ollama_cloud":
            assert candidate.effective_supports_tools is True


def test_configured_tool_less_model_is_skipped_for_tool_tasks() -> None:
    catalog = CapabilityCatalog()
    catalog.register(
        ModelCapability(
            provider="ollama_cloud",
            model_id="tool-less-model",
            tool_calling=False,
            supports_tools=False,
            context_limit=131072,
            coding=3,
        )
    )
    catalog.register(
        ModelCapability(
            provider="ollama_cloud",
            model_id="tool-capable-model",
            tool_calling=True,
            supports_tools=True,
            context_limit=131072,
            coding=2,
        )
    )

    selection = select_model(
        _settings(ollama_coding_model="tool-less-model"),
        "run the tests in my repo",
        catalog=catalog,
        requires_tools=True,
    )

    assert selection.model_id == "tool-capable-model"
    assert "fallback_role" not in selection.reason


def test_coding_task_selects_a_coding_capable_model() -> None:
    selection = select_model(_settings(), "refactor the parser and fix the bug in my repo")
    assert selection.task == TaskFamily.REPOSITORY_WORK
    assert selection.role == ModelRole.CODING
    assert selection.provider == "ollama_cloud"
    cap = DEFAULT_CATALOG.get("ollama_cloud", selection.model_id)
    assert cap is not None and cap.coding >= 2


def test_long_context_task_respects_the_model_context_limit() -> None:
    selection = select_model(_settings(), "read the whole file and summarize this document")

    assert selection.task == TaskFamily.LONG_CONTEXT
    cap = DEFAULT_CATALOG.get(selection.provider, selection.model_id)
    assert cap is not None
    assert cap.context_limit >= rt.LARGE_CONTEXT_TOKENS


def test_disabled_role_degrades_without_crashing() -> None:
    """An unavailable role must disable, never break the whole runtime."""
    catalog = CapabilityCatalog()
    catalog.register(
        ModelCapability(
            provider="ollama_cloud",
            model_id="general-only",
            tool_calling=True,
            supports_tools=True,
            context_limit=16384,
        )
    )
    settings = _settings(ollama_coding_model="ghost-model")

    roles = resolve_role_models(settings, catalog, requires_tools=True)
    assert roles.get(ModelRole.GENERAL) == ("general-only", "discovered")
    assert roles.get(ModelRole.CODING) == ("general-only", "discovered")

    diagnostics = validate_configuration(settings, catalog)
    assert "ghost-model" in diagnostics["unknown_configured_models"]


def test_task_classification_is_deterministic() -> None:
    assert classify_task("hi") == TaskFamily.CHAT
    assert classify_task("fix the bug in my repo") == TaskFamily.REPOSITORY_WORK
    assert classify_task("why does this design work") == TaskFamily.REASONING
    assert classify_task("convert this json to yaml") == TaskFamily.SIMPLE_TRANSFORMATION
    assert classify_task("read the whole file") == TaskFamily.LONG_CONTEXT
    assert classify_task("search the web for pytest docs") == TaskFamily.RESEARCH


# ---------------------------------------------------------------------------
# Offline / primary resolution
# ---------------------------------------------------------------------------


def test_offline_provider_stays_authoritative() -> None:
    settings = _settings(default_provider="echo")
    assert effective_primary(settings) == ("echo", "offline_authoritative")
    selection = select_model(settings, "hello")
    assert selection.provider == "echo"
    assert selection.candidates == (("echo", "echo-default"),)


def test_ollama_cloud_is_the_default_primary() -> None:
    settings = _settings()
    assert effective_primary(settings) == ("ollama_cloud", "configured_primary")
    selection = select_model(settings, "hello")
    assert selection.provider == "ollama_cloud"
    assert selection.is_ollama_cloud is True


def test_missing_ollama_credential_keeps_the_deployment_running() -> None:
    settings = _settings(ollama_cloud_api_key=None, default_provider="groq")
    assert effective_primary(settings) == ("groq", "ollama_cloud_unconfigured")


def test_describe_selection_is_compact_and_credential_free() -> None:
    label = describe_selection(select_model(_settings(), "hello"))
    assert label.startswith("⚙️ Ollama Cloud · ")
    assert "test-ollama-key" not in label


def test_health_states_follow_real_requests() -> None:
    health = InferenceHealthTracker()
    assert health.state_for("ollama_cloud", "m") == HealthState.HEALTHY
    health.report_failure("ollama_cloud", "m", ErrorKind.TIMEOUT, "timed out")
    assert health.state_for("ollama_cloud", "m") == HealthState.DEGRADED
    health.report_failure("ollama_cloud", "m", ErrorKind.SERVER_ERROR, "503")
    assert health.state_for("ollama_cloud", "m") == HealthState.TEMPORARILY_UNAVAILABLE
    health.report_failure("ollama_cloud", "m", ErrorKind.AUTH_FAILED, "401")
    assert health.state_for("ollama_cloud", "m") == HealthState.UNAVAILABLE
    assert health.is_available("ollama_cloud", "m") is False
    health.report_success("ollama_cloud", "m")
    assert health.state_for("ollama_cloud", "m") == HealthState.HEALTHY
    assert health.snapshot()[0]["model_id"] == "m"


def test_health_is_never_polled_without_requests() -> None:
    """No periodic API health checks: an idle tracker stays empty."""
    assert InferenceHealthTracker().snapshot() == []


def test_selection_json_is_inspectable() -> None:
    selection: ModelSelection = select_model(_settings(), "hello")
    payload = selection.to_json()
    assert payload["provider"] == "ollama_cloud"
    assert payload["task"] == "CHAT"
    assert any(item.startswith("ollama_cloud:") for item in payload["candidates"])


# ---------------------------------------------------------------------------
# Telegram behaviour
# ---------------------------------------------------------------------------


def _presenter(bus: EventBus, outbox: Any, session_id: str) -> Any:
    from agent_system.services.telegram_presenter import TelegramProgressPresenter

    presenter = TelegramProgressPresenter(
        factory=object(),
        outbox=outbox,
        chat_id=4242,
        session_id=session_id,
        bus=bus,
    )
    presenter.start()
    return presenter


def test_presenter_shows_truthful_model_status_without_internal_ids() -> None:
    bus = EventBus()
    outbox = MagicMock()
    outbox.enqueue.return_value = "out_1"
    session_id = "ses_telegram0000000001"
    presenter = _presenter(bus, outbox, session_id)
    outbox.reset_mock()

    bus.emit(
        Event(
            type="inference.model_selected",
            session_id=session_id,
            payload={"label": "⚙️ Ollama Cloud · gpt-oss:120b"},
        ),
        None,
    )

    assert outbox.enqueue.called
    text = outbox.enqueue.call_args[1]["text"]
    assert "Ollama Cloud · gpt-oss:120b" in text
    for marker in ("task_", "ses_", "sk-", "Traceback"):
        assert marker not in text
    presenter.stop()


def test_presenter_notifies_fallback_exactly_once() -> None:
    bus = EventBus()
    outbox = MagicMock()
    session_id = "ses_telegram0000000002"
    presenter = _presenter(bus, outbox, session_id)
    outbox.reset_mock()

    event = Event(
        type="inference.fallback_activated",
        session_id=session_id,
        payload={"notice": rt.FALLBACK_NOTICE, "to_provider": "groq"},
    )
    bus.emit(event, None)
    bus.emit(event, None)  # a duplicate emit must not double-post

    assert outbox.enqueue.call_count == 1
    assert outbox.enqueue.call_args[1]["text"] == rt.FALLBACK_NOTICE
    assert outbox.enqueue.call_args[1]["kind"] == "notification"
    for marker in ("Traceback", "api_key", "task_", "ses_", "sk-"):
        assert marker not in rt.FALLBACK_NOTICE
    presenter.stop()


def lock_key_row(factory: Any) -> str:
    """Model id recorded by the durable session lock (test helper)."""
    with session_scope(factory) as db:
        row = db.query(InferenceModelLock).one_or_none()
        return str(row.model_id) if row is not None else ""


def test_presenter_ignores_events_from_other_sessions() -> None:
    bus = EventBus()
    outbox = MagicMock()
    presenter = _presenter(bus, outbox, "ses_telegram0000000003")
    outbox.reset_mock()

    bus.emit(
        Event(
            type="inference.fallback_activated",
            session_id="ses_someoneelse0000001",
            payload={"notice": rt.FALLBACK_NOTICE},
        ),
        None,
    )

    assert outbox.enqueue.call_count == 0
    presenter.stop()


# ---------------------------------------------------------------------------
# Production path: the agent handler itself
# ---------------------------------------------------------------------------


def test_agent_handler_locks_one_ollama_model_for_the_whole_tool_loop(
    factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: the real handler selects ONCE and never switches models."""
    from agent_system.agents import react_agent

    settings = _settings(
        ollama_coding_model="gpt-oss:120b",
        tools_fs_roots=str(tmp_path),
        tools_shell_mode="off",
        tools_require_approval=False,
        tools_max_iters=4,
        memory_recall_top_k=0,
        openconnector_base_url="",
        mcp_servers="[]",
        skills_dir=tmp_path / "skills",
    )
    (tmp_path / "skills").mkdir(exist_ok=True)
    target = tmp_path / "locked_note.txt"
    first_turn = (
        f'working...\n\n```tool:file_write\n{{"path": "{target}", "content": "locked"}}\n```\n'
    )
    seen: list[str] = []

    class _StubRouter:
        def _answer(self, model_id: str) -> Any:
            seen.append(model_id)
            text = first_turn if len(seen) == 1 else "done"
            return SimpleNamespace(
                ok=True,
                output=text,
                error=None,
                tokens_in=1,
                tokens_out=1,
                tokens_cached=0,
                tool_calls=None,
            )

        def invoke(self, _factory: Any, model_id: str, _prompt: str, **_kw: Any) -> Any:
            return self._answer(model_id)

        def invoke_streaming(self, _factory: Any, model_id: str, _prompt: str, **_kw: Any) -> Any:
            return self._answer(model_id)

    monkeypatch.setattr(react_agent, "_build_router", lambda _s, _b: _StubRouter())

    result = react_agent.llm_react_handler(
        {"goal": "audit the repository code"},
        {
            "session_id": "ses_handler00000000001",
            "task_id": "task_handler0000000001",
            "agent_run_id": "run_handler00000000001",
            "agent_type": "llm",
            "factory": factory,
            "settings": settings,
        },
    )

    assert result["output"] == "done"
    assert result["tool_calls"] == 1
    # Every iteration of the tool loop used the SAME locked model. Which
    # provider wins is the strategic chain's call (the fixture configures
    # groq/gemini/openrouter too), so assert the locking invariant itself.
    assert len(seen) == 2
    assert len(set(seen)) == 1
    # The decision and the lock are durable and observable.
    with session_scope(factory) as db:
        types = {row.type for row in db.query(EventRow).all()}
    assert "inference.model_selected" in types
    # The persisted lock is the same model the loop actually used.
    assert lock_key_row(factory) == seen[0]
