"""Model router + cost accounting (v3.1 §19–§20).

Provider adapters with external configuration — no hardcoded fictional model
IDs. Every invocation records a ModelCall row. Unknown/estimated usage never
crashes execution; it is marked explicitly.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from agent_system.domain import ids
from agent_system.domain.events import Event
from agent_system.infra.db import session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import ModelCall


@dataclass(frozen=True)
class ModelInfo:
    model_id: str
    provider: str
    input_cost_per_1m: float  # USD
    output_cost_per_1m: float
    context_window: int = 128_000
    capabilities: tuple[str, ...] = ("general",)


@dataclass
class PricingRegistry:
    _models: dict[str, ModelInfo] = field(default_factory=dict)

    def register(self, info: ModelInfo) -> None:
        self._models[info.model_id] = info

    def get(self, model_id: str) -> ModelInfo | None:
        return self._models.get(model_id)

    def all(self) -> list[ModelInfo]:
        return list(self._models.values())

    def estimate_cost(self, model_id: str, tokens_in: int, tokens_out: int) -> float | None:
        info = self._models.get(model_id)
        if info is None:
            return None
        return (tokens_in / 1_000_000) * info.input_cost_per_1m + (
            tokens_out / 1_000_000
        ) * info.output_cost_per_1m


@dataclass
class SelectionRule:
    task_type: str
    primary: str
    fallback: str | None = None
    budget_tier: str | None = None


@dataclass
class ModelRegistry:
    """External configuration (loaded from JSON, never hardcoded fiction)."""

    _rules: dict[str, SelectionRule] = field(default_factory=dict)

    def set_rule(self, rule: SelectionRule) -> None:
        self._rules[rule.task_type] = rule

    def rule_for(self, task_type: str) -> SelectionRule | None:
        return self._rules.get(task_type)

    def select(self, task_type: str, mode: str = "primary") -> str | None:
        rule = self._rules.get(task_type)
        if rule is None:
            return None
        chosen: str | None = {
            "primary": rule.primary,
            "fallback": rule.fallback,
            "budget": rule.budget_tier,
        }.get(mode, rule.primary)
        if chosen is None:
            return None
        return chosen


@dataclass
class InvocationResult:
    model_call_id: str
    model_id: str
    provider: str
    ok: bool
    tokens_in: int | None
    tokens_out: int | None
    tokens_cached: int | None
    usage_is_estimated: bool
    cost_usd: float | None
    cost_is_estimated: bool
    latency_ms: int
    output: str | None = None
    error: str | None = None


class ProviderAdapter:
    """Interface every provider adapter implements."""

    supports_streaming: bool = False

    def invoke(self, model_id: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError

    def stream(self, model_id: str, prompt: str, **kwargs: Any) -> Any:
        """Yield text deltas; return (chunks, usage). Only when supported."""
        raise NotImplementedError


class EchoProvider(ProviderAdapter):
    """Deterministic local adapter for tests/offline mode (no network)."""

    supports_streaming = True

    def invoke(self, model_id: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        text = f"[{model_id}] echo:{len(prompt)} chars"
        return {
            "output": text,
            "usage": {
                "input_tokens": len(prompt) // 4,
                "output_tokens": len(text) // 4,
                "cached_tokens": 0,
            },
        }

    def stream(self, model_id: str, prompt: str, **kwargs: Any) -> Any:
        """Yield the echo text word-by-word (offline streaming demo/tests)."""
        result = self.invoke(model_id, prompt, **kwargs)
        words = str(result["output"]).split(" ")

        def _chunks() -> Any:
            for i, word in enumerate(words):
                yield word + (" " if i < len(words) - 1 else "")

        return _chunks(), dict(result["usage"])


class UnavailableProvider(ProviderAdapter):
    """Simulates provider outage (for recovery tests)."""

    def invoke(self, model_id: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        raise ConnectionError("provider unreachable")


class ProviderUnavailableError(RuntimeError):
    """Fail-fast error raised while a provider's circuit is OPEN."""


class ProviderCircuitBreaker:
    """Per-provider circuit breaker (CLOSED → OPEN → HALF_OPEN).

    Tracks consecutive adapter failures. After ``threshold`` consecutive
    failures the circuit trips to OPEN for ``cooldown_seconds`` — calls
    during OPEN fail fast with ``ProviderUnavailableError`` (no network
    call, never a fabricated response). After the cooldown exactly one
    half-open probe is allowed through: success closes the circuit,
    failure re-opens it and doubles the cooldown (capped at 10 minutes).

    Thread-safe; the clock is injectable for deterministic tests.
    """

    MAX_COOLDOWN_SECONDS = 600.0

    def __init__(
        self,
        provider: str,
        threshold: int = 5,
        cooldown_seconds: float = 60.0,
        clock: Any | None = None,
    ) -> None:
        import time as _time

        self.provider = provider
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self._base_cooldown = cooldown_seconds
        self._clock = clock or _time.monotonic
        self._lock = threading.Lock()
        self._failures = 0
        self._state = "closed"
        self._opened_at = 0.0
        self._probe_in_flight = False

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self._failures

    def before_call(self) -> None:
        """Raise ProviderUnavailableError when the circuit is OPEN."""
        with self._lock:
            if self._state == "closed":
                return
            now = self._clock()
            if self._state == "open":
                if now - self._opened_at < self.cooldown_seconds:
                    raise ProviderUnavailableError(
                        f"provider '{self.provider}' circuit is open "
                        f"(cooldown {self.cooldown_seconds:.0f}s)"
                    )
                self._state = "half-open"
                self._probe_in_flight = True
                return
            # half-open: exactly one probe allowed through.
            if self._probe_in_flight:
                raise ProviderUnavailableError(
                    f"provider '{self.provider}' circuit is half-open (probe already in flight)"
                )
            self._probe_in_flight = True

    def after_success(self) -> str | None:
        """Record success; return 'closed' when a non-closed circuit closes."""
        with self._lock:
            reopened = self._state != "closed"
            self._failures = 0
            self._probe_in_flight = False
            self._state = "closed"
            self.cooldown_seconds = self._base_cooldown
            return "closed" if reopened else None

    def after_failure(self) -> str | None:
        """Record failure; return 'opened' when the circuit just tripped."""
        with self._lock:
            self._failures += 1
            self._probe_in_flight = False
            if self._state == "half-open":
                self._state = "open"
                self._opened_at = self._clock()
                self.cooldown_seconds = min(self.cooldown_seconds * 2, self.MAX_COOLDOWN_SECONDS)
                return "opened"
            if self._state == "closed" and self._failures >= self.threshold:
                self._state = "open"
                self._opened_at = self._clock()
                return "opened"
            return None

            return None


class ModelRouter:
    """Routes model calls with cost accounting and an optional daily budget.

    Budget enforcement is intentionally minimal but real (not a stub): the
    router owns a :class:`BudgetMonitor` seeded with ``daily_budget_usd``.
    ``invoke``/``invoke_streaming`` check the daily budget before the adapter
    call (fail fast with ``BudgetExceededError`` when exhausted) and record
    actual spend afterwards, emitting ``cost.alert`` events at 50/75/90/100%.
    """

    def __init__(
        self,
        event_bus: EventBus,
        pricing: PricingRegistry | None = None,
        registry: ModelRegistry | None = None,
        default_provider: str | None = None,
        default_model: str | None = None,
        skill_manager: Any | None = None,
        soul_text: str | None = None,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_cooldown_seconds: float = 60.0,
        daily_budget_usd: float = 10.0,
        budget_monitor: BudgetMonitor | None = None,
    ) -> None:
        self._bus = event_bus
        self.pricing = pricing or PricingRegistry()
        self.registry = registry or ModelRegistry()
        self._adapters: dict[str, ProviderAdapter] = {}
        self._lock = threading.Lock()
        self.default_provider = default_provider or "echo"
        self.default_model = default_model or "echo-default"
        # Optional SkillManager: when set, invoke() can inject skill
        # instructions into the prompt (skills= / agent_type= kwargs).
        self.skill_manager = skill_manager
        # Optional soul: prepended as the <identity> block on every call.
        self.soul_text = soul_text or None
        self.circuit_breaker_threshold = circuit_breaker_threshold
        self.circuit_breaker_cooldown_seconds = circuit_breaker_cooldown_seconds
        self._breakers: dict[str, ProviderCircuitBreaker] = {}
        # Daily budget wiring (minimal but real): shared monitor tracks
        # cumulative spend against daily_budget_usd across all invocations.
        self.daily_budget_usd = daily_budget_usd
        self.budget: BudgetMonitor = budget_monitor or BudgetMonitor()
        if "daily" not in self.budget._limits:  # noqa: SLF001
            self.budget.set_budget("daily", daily_budget_usd)

    def _budget_check(self) -> str | None:
        """Return an error message when the daily budget is exhausted."""
        remaining = self.budget.remaining("daily")
        if remaining is not None and remaining <= 0:
            return (
                f"BudgetExceededError: daily budget ${self.budget._limits.get('daily')} "  # noqa: SLF001
                f"exhausted (spent ${self.budget.spent('daily'):.4f})"
            )
        return None

    def register_adapter(self, provider: str, adapter: ProviderAdapter) -> None:
        with self._lock:
            self._adapters[provider] = adapter

    def _adapter_for(self, model_id: str) -> ProviderAdapter | None:
        info = self.pricing.get(model_id)
        if info is None:
            return None
        return self._adapters.get(info.provider)

    def _breaker_for(self, provider: str) -> ProviderCircuitBreaker:
        """Per-provider breaker (unknown providers are never breakered)."""
        with self._lock:
            breaker = self._breakers.get(provider)
            if breaker is None:
                breaker = ProviderCircuitBreaker(
                    provider,
                    threshold=self.circuit_breaker_threshold,
                    cooldown_seconds=self.circuit_breaker_cooldown_seconds,
                )
                self._breakers[provider] = breaker
            return breaker

    def breaker_state(self, provider: str) -> str | None:
        """Inspect a provider's circuit state (None = no breaker yet)."""
        with self._lock:
            breaker = self._breakers.get(provider)
            return breaker.state if breaker else None

    def invoke(
        self,
        factory: Any,
        model_id: str,
        prompt: str,
        task_id: str | None = None,
        agent_run_id: str | None = None,
        session_id: str | None = None,
        skills: list[str] | None = None,
        agent_type: str | None = None,
        **kwargs: Any,
    ) -> InvocationResult:
        """Invoke a model, recording model.requested/completed/failed + ModelCall.

        When a ``skill_manager`` is attached, ``skills=[...]`` injects exactly
        those skills' instructions (or, when omitted, every enabled skill
        matching ``agent_type``) into the prompt before the adapter call.
        """
        import time

        prompt, skills_used, soul_used = self._compose_prompt(prompt, skills, agent_type)
        info = self.pricing.get(model_id)
        provider = info.provider if info else "unknown"
        call_id = ids.new_model_call_id()
        self._emit_requested(
            factory,
            call_id,
            model_id,
            provider,
            skills_used,
            soul_used,
            session_id,
            task_id,
            agent_run_id,
        )
        adapter = self._adapter_for(model_id)
        started = time.monotonic()
        breaker = self._breaker_for(provider) if provider != "unknown" else None

        def _call() -> tuple[str, dict[str, Any]]:
            assert adapter is not None
            response = adapter.invoke(model_id, prompt, **kwargs)
            return str(response.get("output", "")), dict(response.get("usage", {}))

        budget_error = self._budget_check()
        if budget_error is not None:
            ok: bool = False
            output: str | None = None
            error: str | None = budget_error
            usage: dict[str, Any] = {}
            circuit_events: list[tuple[str, dict[str, Any]]] = []
        elif adapter is None:
            ok = False
            output = None
            error = f"no adapter registered for provider '{provider}'"
            usage = {}
            circuit_events = []
        else:
            ok, output, error, usage, circuit_events = self._run_guarded(breaker, _call)
        latency_ms = int((time.monotonic() - started) * 1000)
        return self._record(
            factory,
            call_id,
            model_id,
            provider,
            ok,
            output,
            usage,
            error,
            latency_ms,
            skills_used,
            soul_used,
            session_id,
            task_id,
            agent_run_id,
            circuit_events,
        )

    def invoke_streaming(
        self,
        factory: Any,
        model_id: str,
        prompt: str,
        task_id: str | None = None,
        agent_run_id: str | None = None,
        session_id: str | None = None,
        skills: list[str] | None = None,
        agent_type: str | None = None,
        on_token: Any | None = None,
        **kwargs: Any,
    ) -> InvocationResult:
        """Streaming twin of ``invoke`` (new path, not a replacement).

        Adapters with ``supports_streaming`` yield text deltas: each chunk
        fires a ``model.token`` event (visibility=user) via ``on_token`` and
        on the bus as it arrives. ``model.completed`` still fires once at the
        end with full usage/cost — accounting is identical to ``invoke``.
        Adapters without streaming fall back to one ``invoke`` + a single
        ``model.token`` carrying the full output, so consumers see the same
        event shape either way. Echo/offline mode streams word-by-word.
        """
        import time

        prompt, skills_used, soul_used = self._compose_prompt(prompt, skills, agent_type)
        info = self.pricing.get(model_id)
        provider = info.provider if info else "unknown"
        call_id = ids.new_model_call_id()
        self._emit_requested(
            factory,
            call_id,
            model_id,
            provider,
            skills_used,
            soul_used,
            session_id,
            task_id,
            agent_run_id,
        )
        adapter = self._adapter_for(model_id)
        started = time.monotonic()
        breaker = self._breaker_for(provider) if provider != "unknown" else None
        budget_error = self._budget_check()
        if budget_error is not None:
            latency_ms = int((time.monotonic() - started) * 1000)
            return self._record(
                factory,
                call_id,
                model_id,
                provider,
                False,
                None,
                {},
                budget_error,
                latency_ms,
                skills_used,
                soul_used,
                session_id,
                task_id,
                agent_run_id,
                [],
            )
        if adapter is None:
            latency_ms = int((time.monotonic() - started) * 1000)
            return self._record(
                factory,
                call_id,
                model_id,
                provider,
                False,
                None,
                {},
                f"no adapter registered for provider '{provider}'",
                latency_ms,
                skills_used,
                soul_used,
                session_id,
                task_id,
                agent_run_id,
                [],
            )

        index = {"n": 0}

        def _emit_chunk(delta: str) -> None:
            self._emit_token(
                factory,
                call_id,
                model_id,
                provider,
                delta,
                index["n"],
                session_id,
                task_id,
                agent_run_id,
                on_token,
            )
            index["n"] += 1

        def _call() -> tuple[str, dict[str, Any]]:
            assert adapter is not None
            if getattr(adapter, "supports_streaming", False):
                chunks, stream_usage = adapter.stream(model_id, prompt, **kwargs)
                parts: list[str] = []
                for delta in chunks:
                    parts.append(delta)
                    _emit_chunk(delta)
                return "".join(parts), dict(stream_usage)
            response = adapter.invoke(model_id, prompt, **kwargs)
            full = str(response.get("output", ""))
            _emit_chunk(full)  # fallback: same event shape, one chunk
            return full, dict(response.get("usage", {}))

        ok, output, error, usage, circuit_events = self._run_guarded(breaker, _call)
        latency_ms = int((time.monotonic() - started) * 1000)
        return self._record(
            factory,
            call_id,
            model_id,
            provider,
            ok,
            output,
            usage,
            error,
            latency_ms,
            skills_used,
            soul_used,
            session_id,
            task_id,
            agent_run_id,
            circuit_events,
        )

    # -- shared invoke machinery (invoke + invoke_streaming) ------------

    def _compose_prompt(
        self,
        prompt: str,
        skills: list[str] | None,
        agent_type: str | None,
    ) -> tuple[str, list[str], bool]:
        skills_used: list[str] = []
        if (skills is not None or agent_type is not None) and self.skill_manager is not None:
            composed = self.skill_manager.compose(prompt, agent_type=agent_type, skills=skills)
            prompt = composed.text
            skills_used = list(composed.skills_used)
        soul_used = False
        if self.soul_text:
            from agent_system.services.soul import identity_block

            prompt = f"{identity_block(self.soul_text)}\n\n{prompt}"
            soul_used = True
        return prompt, skills_used, soul_used

    def _emit_requested(
        self,
        factory: Any,
        call_id: str,
        model_id: str,
        provider: str,
        skills_used: list[str],
        soul_used: bool,
        session_id: str | None,
        task_id: str | None,
        agent_run_id: str | None,
    ) -> None:
        with session_scope(factory) as db:
            self._bus.emit(
                Event(
                    type="model.requested",
                    session_id=session_id,
                    task_id=task_id,
                    agent_run_id=agent_run_id,
                    actor=provider,
                    payload={
                        "model_id": model_id,
                        "model_call_id": call_id,
                        "skills_used": skills_used,
                        "soul_used": soul_used,
                    },
                ),
                db,
            )

    def _run_guarded(
        self,
        breaker: ProviderCircuitBreaker | None,
        call: Any,
    ) -> tuple[bool, str | None, str | None, dict[str, Any], list[tuple[str, dict[str, Any]]]]:
        """Run ``call`` under the circuit breaker (shared by both paths).

        Returns (ok, output, error, usage, circuit_events). Fast-fail while
        OPEN performs no network call and never fabricates a response.
        """
        circuit_events: list[tuple[str, dict[str, Any]]] = []
        if breaker is None:
            try:
                output, usage = call()
                return True, output, None, usage, circuit_events
            except Exception as exc:
                return False, None, f"{type(exc).__name__}: {exc}", {}, circuit_events
        try:
            breaker.before_call()
        except ProviderUnavailableError as exc:
            return False, None, f"{type(exc).__name__}: {exc}", {}, circuit_events
        try:
            output, usage = call()
            ok = True
            error = None
        except Exception as exc:
            output, usage, ok, error = None, {}, False, f"{type(exc).__name__}: {exc}"
        transition = breaker.after_success() if ok else breaker.after_failure()
        if transition == "opened":
            circuit_events.append(
                (
                    "model.circuit_opened",
                    {
                        "provider": breaker.provider,
                        "consecutive_failures": breaker.consecutive_failures,
                        "cooldown_seconds": breaker.cooldown_seconds,
                    },
                )
            )
        elif transition == "closed":
            circuit_events.append(("model.circuit_closed", {"provider": breaker.provider}))
        return ok, output, error, usage, circuit_events

    def _emit_token(
        self,
        factory: Any,
        call_id: str,
        model_id: str,
        provider: str,
        delta: str,
        index: int,
        session_id: str | None,
        task_id: str | None,
        agent_run_id: str | None,
        on_token: Any | None,
    ) -> None:
        with session_scope(factory) as db:
            self._bus.emit(
                Event(
                    type="model.token",
                    session_id=session_id,
                    task_id=task_id,
                    agent_run_id=agent_run_id,
                    actor=provider,
                    payload={
                        "model_call_id": call_id,
                        "model_id": model_id,
                        "delta": delta,
                        "index": index,
                    },
                ),
                db,
            )
        if on_token is not None:
            try:
                on_token(delta)
            except Exception:
                pass  # token callbacks must never break the model call

    def _record(
        self,
        factory: Any,
        call_id: str,
        model_id: str,
        provider: str,
        ok: bool,
        output: str | None,
        usage: dict[str, Any],
        error: str | None,
        latency_ms: int,
        skills_used: list[str],
        soul_used: bool,
        session_id: str | None,
        task_id: str | None,
        agent_run_id: str | None,
        circuit_events: list[tuple[str, dict[str, Any]]],
    ) -> InvocationResult:
        tokens_in = usage.get("input_tokens")
        tokens_out = usage.get("output_tokens")
        tokens_cached = usage.get("cached_tokens")
        usage_estimated = not usage  # no usage data -> explicitly estimated
        cost = self.pricing.estimate_cost(
            model_id,
            int(tokens_in or 0),
            int(tokens_out or 0),
        )
        # Unknown pricing -> None cost, flagged estimated; NEVER crashes.
        cost_estimated = cost is None
        from agent_system.infra.telemetry import get_metrics

        get_metrics().record_model_latency(provider, latency_ms, ok)
        if session_id is not None and cost is not None:
            get_metrics().record_cost(session_id, cost)

        with session_scope(factory) as db:
            db.add(
                ModelCall(
                    id=call_id,
                    task_id=task_id,
                    agent_run_id=agent_run_id,
                    provider=provider,
                    model_id=model_id,
                    status="ok" if ok else "failed",
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    tokens_cached=tokens_cached,
                    usage_is_estimated=usage_estimated,
                    cost_usd=cost,
                    cost_is_estimated=cost_estimated,
                    latency_ms=latency_ms,
                    error_json={"error": error} if error else None,
                )
            )
            self._bus.emit(
                Event(
                    type="model.completed" if ok else "model.failed",
                    session_id=session_id,
                    task_id=task_id,
                    agent_run_id=agent_run_id,
                    actor=provider,
                    payload={
                        "model_call_id": call_id,
                        "model_id": model_id,
                        "cost_usd": cost,
                        "cost_is_estimated": cost_estimated,
                        "latency_ms": latency_ms,
                        "skills_used": skills_used,
                        "soul_used": soul_used,
                        **({"error": error} if error else {}),
                    },
                ),
                db,
            )
            if cost is not None:
                self._bus.emit(
                    Event(
                        type="cost.recorded",
                        session_id=session_id,
                        task_id=task_id,
                        actor="cost_tracker",
                        payload={"model_call_id": call_id, "cost_usd": cost, "model_id": model_id},
                    ),
                    db,
                )
                # Record spend against the daily budget (minimal wiring:
                # check happens before the call, spend recorded here).
                try:
                    fired = self.budget.record("daily", cost)
                except Exception:
                    fired = []
                for level in fired:
                    self._bus.emit(
                        Event(
                            type="cost.alert",
                            session_id=session_id,
                            task_id=task_id,
                            actor="cost_tracker",
                            payload={
                                "scope": "daily",
                                "level_pct": level,
                                "spent_usd": self.budget.spent("daily"),
                                "limit_usd": self.budget._limits.get("daily"),  # noqa: SLF001
                                "model_call_id": call_id,
                            },
                        ),
                        db,
                    )
            for event_type, payload in circuit_events:
                self._bus.emit(
                    Event(
                        type=event_type,
                        session_id=session_id,
                        task_id=task_id,
                        agent_run_id=agent_run_id,
                        actor=provider,
                        payload={"model_id": model_id, **payload},
                    ),
                    db,
                )

        return InvocationResult(
            model_call_id=call_id,
            model_id=model_id,
            provider=provider,
            ok=ok,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            tokens_cached=tokens_cached,
            usage_is_estimated=usage_estimated,
            cost_usd=cost,
            cost_is_estimated=cost_estimated,
            latency_ms=latency_ms,
            output=output,
            error=error,
        )


class BudgetMonitor:
    """Budget levels 50/75/90/100% across scopes (v3.1 §20)."""

    LEVELS = (50.0, 75.0, 90.0, 100.0)

    def __init__(self) -> None:
        self._spent: dict[str, float] = {}
        self._limits: dict[str, float] = {}
        self._alerted: dict[str, set[float]] = {}

    def set_budget(self, scope: str, limit_usd: float) -> None:
        self._limits[scope] = limit_usd
        self._spent.setdefault(scope, 0.0)
        self._alerted.setdefault(scope, set())

    def record(self, scope: str, cost_usd: float) -> list[float]:
        """Record spend; return triggered alert levels (each fires once)."""
        self._spent[scope] = self._spent.get(scope, 0.0) + cost_usd
        limit = self._limits.get(scope)
        if not limit:
            return []
        pct = (self._spent[scope] / limit) * 100
        fired = [lvl for lvl in self.LEVELS if pct >= lvl and lvl not in self._alerted[scope]]
        self._alerted[scope].update(fired)
        return fired

    def spent(self, scope: str) -> float:
        return self._spent.get(scope, 0.0)

    def remaining(self, scope: str) -> float | None:
        limit = self._limits.get(scope)
        return None if limit is None else limit - self._spent.get(scope, 0.0)


#: Shared process-wide daily budget monitor. Routers created without an
#: explicit ``budget_monitor`` get their own instance seeded from settings;
#: this global is available for cross-router accounting if callers opt in.
global_budget_monitor = BudgetMonitor()
global_budget_monitor.set_budget("daily", 10.0)
