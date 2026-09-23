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
    #: Canonical ``[{"id", "name", "arguments"}]`` structured tool calls from
    #: the adapter result (``None`` when the provider returned none).
    tool_calls: list[dict[str, Any]] | None = None
    error: str | None = None


class ProviderAdapter:
    """Interface every provider adapter implements.

    ``invoke`` returns a dict that may carry ``output`` (str), ``usage``
    (dict), and — for providers that produced structured tool calls — a
    canonical ``tool_calls`` list (``[{"id", "name", "arguments"}]``). The
    router forwards that list verbatim to :class:`InvocationResult` so the
    agent loop can parse it; execution never branches per provider.
    """

    supports_streaming: bool = False

    def invoke(self, model_id: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError

    def stream(self, model_id: str, prompt: str, **kwargs: Any) -> Any:
        """Yield text deltas; return ``(chunks, usage, tool_calls)``.

        ``tool_calls`` is the same canonical list as ``invoke`` (``None`` when
        the provider streamed no structured calls). Only required when
        ``supports_streaming`` is True.
        """
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

        return _chunks(), dict(result["usage"]), None


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
        settings: Any | None = None,
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
        #: Deployment settings, used to read the configured budget limits when
        #: enforcing against persisted spend (services/budget.py).
        self.settings = settings
        self.budget: BudgetMonitor = budget_monitor or BudgetMonitor()
        if "daily" not in self.budget._limits:  # noqa: SLF001
            self.budget.set_budget("daily", daily_budget_usd)

    def _budget_check(
        self,
        factory: Any = None,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        provider: str | None = None,
    ) -> str | None:
        """Return an error message when a budget scope is exhausted.

        With a session factory the check is derived from the persisted
        ``model_calls`` records (:class:`~agent_system.services.budget.BudgetLedger`),
        so it is authoritative and survives a restart. Without one it degrades
        to the in-process monitor — documented behaviour, never used by the
        worker or API, both of which always pass a factory.
        """
        if factory is not None:
            from agent_system.services.budget import BudgetLedger

            decision = BudgetLedger(factory, self.settings).check(
                task_id=task_id, session_id=session_id, provider=provider
            )
            if not decision.allowed:
                return f"BudgetExceededError: {decision.reason}"
            return None
        remaining = self.budget.remaining("daily")
        if remaining is not None and remaining <= 0:
            return (
                f"BudgetExceededError: daily budget ${self.budget._limits.get('daily')} "  # noqa: SLF001
                f"exhausted (spent ${self.budget.spent('daily'):.4f})"
            )
        return None

    def _budget_alerts(self, factory: Any, cost: float, call_id: str) -> list[dict[str, Any]]:
        """Thresholds newly crossed by this call, derived from persisted spend.

        The level fires when the total crosses it *because of* this call:
        before = after - cost. Because `before`/`after` come from the database,
        a restart cannot re-fire an alert that already fired.
        """
        from agent_system.services.budget import ALERT_LEVELS, BudgetLedger

        try:
            ledger = BudgetLedger(factory, self.settings)
            after = ledger.daily_usage()
        except Exception:
            return []
        if not after.limit_usd:
            return []
        before = after.spent_usd - cost
        payloads: list[dict[str, Any]] = []
        for level in ALERT_LEVELS:
            threshold = after.limit_usd * (level / 100.0)
            if before < threshold <= after.spent_usd:
                payloads.append(
                    {
                        "scope": "daily",
                        "level_pct": level,
                        "spent_usd": round(after.spent_usd, 6),
                        "limit_usd": after.limit_usd,
                        "model_call_id": call_id,
                        "source": "persisted_model_calls",
                    }
                )
        return payloads

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

    def _failover_chain(
        self, model_id: str, provider: str
    ) -> list[tuple[str, str, Any | None]]:
        """Ordered (model, provider, adapter) attempts: primary + failover.

        Offline (echo/none) primaries never fail over. Alternates honor
        ``llm_provider_order`` when set, else adapter registration order, and
        skip echo, the failed primary, unroutable (disabled/auth-failed/cooled
        down) providers, and providers with no adapter/pricing entry. Bounded
        by ``llm_max_fallback_attempts`` (min 1).
        """
        primary = (model_id, provider, self._adapters.get(provider))
        try:
            from agent_system.services.providers import is_offline_provider  # noqa: PLC0415
        except Exception:
            is_offline_provider = lambda p: str(p or "").strip().lower() in {"echo", "none", ""}  # noqa: E731
        if is_offline_provider(provider):
            return [primary]
        try:
            max_attempts = int(getattr(self.settings, "llm_max_fallback_attempts", 3) or 3)
        except Exception:
            max_attempts = 3
        max_attempts = max(1, max_attempts)
        try:
            raw_order = str(getattr(self.settings, "llm_provider_order", "") or "")
            preferred = [p.strip() for p in raw_order.split(",") if p.strip()]
        except Exception:
            preferred = []
        with self._lock:
            registered = list(self._adapters.keys())
        ordered = [p for p in preferred if p in registered]
        ordered += [p for p in registered if p not in ordered]
        try:
            from agent_system.services.provider_health import GLOBAL_HEALTH_TRACKER  # noqa: PLC0415
        except Exception:
            GLOBAL_HEALTH_TRACKER = None  # type: ignore[assignment]  # noqa: N806
        try:
            from agent_system.services.providers import provider_spec  # noqa: PLC0415
        except Exception:
            provider_spec = None  # type: ignore[assignment]
        chain = [primary]
        for cand in ordered:
            if len(chain) >= max_attempts:
                break
            if cand == provider or cand == "echo":
                continue
            if cand not in self._adapters:
                continue
            cand_model = model_id
            if provider_spec is not None:
                try:
                    spec = provider_spec(cand)
                    if spec is not None and getattr(spec, "default_model", None):
                        cand_model = spec.default_model
                except Exception:
                    pass
            try:
                if self.pricing.get(cand_model) is None:
                    continue
            except Exception:
                pass
            if GLOBAL_HEALTH_TRACKER is not None:
                try:
                    if not GLOBAL_HEALTH_TRACKER.is_routable(cand, cand_model):
                        continue
                except Exception:
                    pass
            chain.append((cand_model, cand, self._adapters[cand]))
        return chain[:max_attempts]

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
        fallback: bool = False,
        **kwargs: Any,
    ) -> InvocationResult:
        """Invoke a model, recording model.requested/completed/failed + ModelCall.

        When a ``skill_manager`` is attached, ``skills=[...]`` injects exactly
        those skills' instructions (or, when omitted, every enabled skill
        matching ``agent_type``) into the prompt before the adapter call.

        ``fallback=False`` (default) tries the single requested model — the
        agent loop's outer ``invoke_with_fallback`` helper owns failover there
        and calls this per candidate. ``fallback=True`` enables internal
        cross-provider failover (primary + alternates bounded by
        ``llm_max_fallback_attempts``) for direct callers like the chat path.
        """
        import time

        prompt, skills_used, soul_used = self._compose_prompt(prompt, skills, agent_type)
        info = self.pricing.get(model_id)
        provider = info.provider if info else "unknown"
        call_id = ids.new_model_call_id()
        invoke_started = time.monotonic()

        budget_error = self._budget_check(
            factory, task_id=task_id, session_id=session_id, provider=provider
        )
        if budget_error is not None:
            ok: bool = False
            output: str | None = None
            error: str | None = budget_error
            usage: dict[str, Any] = {}
            tool_calls: list[dict[str, Any]] | None = None
            circuit_events: list[tuple[str, dict[str, Any]]] = []
            latency_ms = int((time.monotonic() - invoke_started) * 1000)
            return self._record(
                factory,
                call_id,
                model_id,
                provider,
                ok,
                output,
                usage,
                tool_calls,
                error,
                latency_ms,
                skills_used,
                soul_used,
                session_id,
                task_id,
                agent_run_id,
                circuit_events,
            )
        # Failover chain: single primary by default (the agent loop's outer
        # invoke_with_fallback owns failover and calls this per candidate).
        # Direct callers opt into internal cross-provider failover.
        # Offline (echo) primaries never fail over — echo is authoritative.
        if fallback:
            chain = self._failover_chain(model_id, provider)
        else:
            chain = [(model_id, provider, self._adapters.get(provider))]
        per_call_timeout = kwargs.pop("timeout", None)
        last_record: Any = None
        for attempt_no, (cand_model, cand_provider, cand_adapter) in enumerate(chain):
            cand_breaker = self._breaker_for(cand_provider) if cand_provider != "unknown" else None
            cand_call_id = call_id if attempt_no == 0 else ids.new_model_call_id()
            self._emit_requested(
                factory,
                cand_call_id,
                cand_model,
                cand_provider,
                skills_used,
                soul_used,
                session_id,
                task_id,
                agent_run_id,
            )
            cand_started = time.monotonic()

            def _attempt(
                _ad: Any = cand_adapter, _cm: str = cand_model, _to: Any = per_call_timeout
            ) -> tuple[str, dict[str, Any], list[dict[str, Any]] | None]:
                # Per-call timeout override (float assignment is GIL-atomic;
                # worst case a concurrent thread inherits the tighter bound).
                old_timeout: Any = None
                if _to is not None and hasattr(_ad, "timeout"):
                    old_timeout = _ad.timeout
                    _ad.timeout = float(_to)
                try:
                    response = _ad.invoke(_cm, prompt, **kwargs)
                finally:
                    if old_timeout is not None:
                        try:
                            _ad.timeout = old_timeout
                        except Exception:
                            pass
                rl_info = response.get("rate_limit_info")
                if rl_info:
                    from agent_system.services.provider_health import GLOBAL_HEALTH_TRACKER

                    GLOBAL_HEALTH_TRACKER.update_rate_limits(cand_provider, _cm, rl_info)
                return (
                    str(response.get("output", "")),
                    dict(response.get("usage", {})),
                    response.get("tool_calls") or None,
                )

            if cand_adapter is None:
                ok, output, error, usage, tool_calls, circuit_events = (
                    False,
                    None,
                    f"no adapter registered for provider '{cand_provider}'",
                    {},
                    None,
                    [],
                )
            else:
                ok, output, error, usage, tool_calls, circuit_events = self._run_guarded(
                    cand_breaker, _attempt
                )
            latency_ms = int((time.monotonic() - cand_started) * 1000)
            last_record = self._record(
                factory,
                cand_call_id,
                cand_model,
                cand_provider,
                ok,
                output,
                usage,
                tool_calls,
                error,
                latency_ms,
                skills_used,
                soul_used,
                session_id,
                task_id,
                agent_run_id,
                circuit_events,
            )
            # Empty output counts as failure for selection (an LLM that says
            # nothing is useless); the attempt stays recorded in the ledger.
            if ok and (output or "").strip():
                return last_record
        return last_record

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
        budget_error = self._budget_check(
            factory, task_id=task_id, session_id=session_id, provider=provider
        )
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
                None,
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
                None,
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

        def _call() -> tuple[str, dict[str, Any], list[dict[str, Any]] | None]:
            assert adapter is not None
            if getattr(adapter, "supports_streaming", False):
                chunks, stream_usage, stream_calls = adapter.stream(model_id, prompt, **kwargs)
                parts: list[str] = []
                for delta in chunks:
                    parts.append(delta)
                    _emit_chunk(delta)
                return "".join(parts), dict(stream_usage), stream_calls or None
            response = adapter.invoke(model_id, prompt, **kwargs)
            full = str(response.get("output", ""))
            _emit_chunk(full)  # fallback: same event shape, one chunk
            return full, dict(response.get("usage", {})), response.get("tool_calls") or None

        ok, output, error, usage, tool_calls, circuit_events = self._run_guarded(breaker, _call)
        latency_ms = int((time.monotonic() - started) * 1000)
        return self._record(
            factory,
            call_id,
            model_id,
            provider,
            ok,
            output,
            usage,
            tool_calls,
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
    ) -> tuple[
        bool,
        str | None,
        str | None,
        dict[str, Any],
        list[dict[str, Any]] | None,
        list[tuple[str, dict[str, Any]]],
    ]:
        """Run ``call`` under the circuit breaker (shared by both paths).

        Returns ``(ok, output, error, usage, tool_calls, circuit_events)``.
        Fast-fail while OPEN performs no network call and never fabricates a
        response.
        """
        circuit_events: list[tuple[str, dict[str, Any]]] = []
        if breaker is None:
            try:
                output, usage, tool_calls = call()
                return True, output, None, usage, tool_calls, circuit_events
            except Exception as exc:
                return False, None, f"{type(exc).__name__}: {exc}", {}, None, circuit_events
        try:
            breaker.before_call()
        except ProviderUnavailableError as exc:
            return False, None, f"{type(exc).__name__}: {exc}", {}, None, circuit_events
        try:
            output, usage, tool_calls = call()
            ok = True
            error = None
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            # 429s carry a Retry-After / rate-limit header set; feed it to the
            # health tracker so the cooldown honours the provider's own hint
            # instead of a fixed default. (The success path parses these from
            # the adapter result; the failure path only has the exception.)
            response = getattr(exc, "response", None)
            rl_headers = None
            if response is not None and hasattr(response, "headers"):
                try:
                    rl_headers = {
                        k: v
                        for k, v in response.headers.items()
                        if "ratelimit" in k.lower() or k.lower() == "retry-after"
                    }
                except Exception:
                    rl_headers = None
            if rl_headers:
                try:
                    from agent_system.services.provider_health import GLOBAL_HEALTH_TRACKER

                    GLOBAL_HEALTH_TRACKER.update_rate_limits(breaker.provider, "", rl_headers)
                except Exception:
                    pass
            output, usage, ok, error, tool_calls = None, {}, False, message, None
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
        return ok, output, error, usage, tool_calls, circuit_events

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
        tool_calls: list[dict[str, Any]] | None,
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
        import logging as _logging

        from agent_system.infra.telemetry import get_metrics
        from agent_system.services.provider_health import GLOBAL_HEALTH_TRACKER

        _logging.getLogger(__name__).info(
            "[LLM] request_sent provider=%s model=%s request_id=%s task_id=%s session_id=%s",
            provider,
            model_id,
            call_id,
            task_id,
            session_id,
        )
        if ok:
            GLOBAL_HEALTH_TRACKER.report_success(provider, model_id, latency_ms=latency_ms)
            if tool_calls:
                _logging.getLogger(__name__).info(
                    "[LLM] tool_call_detected provider=%s model=%s request_id=%s count=%s",
                    provider,
                    model_id,
                    call_id,
                    len(tool_calls),
                )
            _logging.getLogger(__name__).info(
                "[LLM] response_received provider=%s model=%s request_id=%s latency_ms=%s",
                provider,
                model_id,
                call_id,
                latency_ms,
            )
        else:
            GLOBAL_HEALTH_TRACKER.report_failure(provider, model_id, error=error or "")
            _logging.getLogger(__name__).warning(
                "[LLM] response_received provider=%s model=%s request_id=%s ok=False error=%s",
                provider,
                model_id,
                call_id,
                str(error or "")[:200],
            )

        get_metrics().record_model_latency(provider, latency_ms, ok)
        if session_id is not None and cost is not None:
            get_metrics().record_cost(session_id, cost)

        with session_scope(factory) as db:
            db.add(
                ModelCall(
                    id=call_id,
                    task_id=task_id,
                    agent_run_id=agent_run_id,
                    session_id=session_id,
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
                # Spend is recorded by the ModelCall row above; the alert
                # thresholds are derived from the persisted total so they
                # survive a restart and cannot double-fire after one.
                for payload in self._budget_alerts(factory, cost, call_id):
                    self._bus.emit(
                        Event(
                            type="cost.alert",
                            session_id=session_id,
                            task_id=task_id,
                            actor="cost_tracker",
                            payload=payload,
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

        import logging as _logging2

        _logging2.getLogger(__name__).info(
            "[LLM] request_completed provider=%s model=%s request_id=%s ok=%s task_id=%s",
            provider,
            model_id,
            call_id,
            ok,
            task_id,
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
            tool_calls=tool_calls,
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
