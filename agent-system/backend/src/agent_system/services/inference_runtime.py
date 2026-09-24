"""Ollama Cloud-first inference runtime (central inference authority).

Bob is one continuous agent on one selected model — not a free-API roulette.
This module owns the decision layer that used to be scattered between the
provider-order setting, the capability router and the fallback chain:

    Telegram -> Agent Session -> Task Classification -> Ollama Cloud model
    selection -> Session Model Lock -> Agent Loop -> Response

Design rules encoded here (all deterministic and inspectable):

- **Ollama Cloud is primary.** Groq / Gemini / OpenRouter are emergency
  fallbacks, reached only after the locked Ollama model has failed bounded
  retries. Normal operation never switches providers.
- **One model per session.** :class:`SessionModelLock` pins the selected
  provider/model to the session; every later call in that session reuses it
  without re-selecting, unless the model is genuinely unavailable.
- **Selection is task/capability based, never LLM-based.** A task is
  classified by deterministic regexes, mapped to a model role, and matched
  against the capability catalog. A model that cannot call tools is never
  selected for a tool-requiring task.
- **Bounded retry, never a busy loop.** Transient failures retry the SAME
  model with an exponentially growing, capped delay that also honours a
  provider-supplied rate-limit cooldown.
- **Fallback is task-scoped.** A successful fallback does not make the
  provider sticky: the next task re-evaluates Ollama Cloud.
- **Real requests are the health signal.** No periodic health polling.

The module is provider-agnostic by construction: it drives whatever
``ModelRouter`` it is handed (``invoke`` / ``invoke_streaming`` are the only
calls made), so the agent core never learns a provider's wire format.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from agent_system.services.llm_catalog import (
    DEFAULT_CATALOG,
    CapabilityCatalog,
    ModelCapability,
)
from agent_system.services.provider_health import GLOBAL_HEALTH_TRACKER
from agent_system.services.providers import is_offline_provider, provider_spec

logger = logging.getLogger(__name__)

#: The primary inference platform.
PRIMARY_PROVIDER = "ollama_cloud"

#: Concise, user-safe status sent to Telegram when the emergency layer engages.
#: Raw provider errors are never surfaced to the user.
FALLBACK_NOTICE = (
    "⚠️ Ollama Cloud is temporarily unavailable.\nI'm continuing with a compatible fallback model."
)

#: Retry delays are capped the same way the circuit breaker caps its cooldown.
DEFAULT_MAX_DELAY = 20.0


class ModelRole(StrEnum):
    """Configurable model roles — never one hard-coded model for everything."""

    GENERAL = "GENERAL"
    FAST = "FAST"
    CODING = "CODING"
    REASONING = "REASONING"
    LONG_CONTEXT = "LONG_CONTEXT"
    TOOL_USE = "TOOL_USE"


class TaskFamily(StrEnum):
    """Deterministic task families used to pick a role."""

    CHAT = "CHAT"
    CODING = "CODING"
    REPOSITORY_WORK = "REPOSITORY_WORK"
    REASONING = "REASONING"
    RESEARCH = "RESEARCH"
    TOOL_HEAVY = "TOOL_HEAVY"
    LONG_CONTEXT = "LONG_CONTEXT"
    SIMPLE_TRANSFORMATION = "SIMPLE_TRANSFORMATION"


class ErrorKind(StrEnum):
    """Normalized provider-error taxonomy (one vocabulary everywhere)."""

    AUTH_FAILED = "AUTH_FAILED"
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    CONTEXT_TOO_LARGE = "CONTEXT_TOO_LARGE"
    TOOL_UNSUPPORTED = "TOOL_UNSUPPORTED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    SERVER_ERROR = "SERVER_ERROR"
    INVALID_REQUEST = "INVALID_REQUEST"
    #: The account/plan cannot use this model at all (HTTP 402 "Payment
    #: Required", an exhausted quota, a lapsed subscription). It is an
    #: *access* fact, not a blip: retrying the same model can never succeed,
    #: so the recovery is to select another compatible model. The shared
    #: ``llm_contract`` taxonomy already carries this code — it is surfaced
    #: here as a first-class runtime kind rather than folded into
    #: RATE_LIMITED, which would wrongly retry it.
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    UNKNOWN = "UNKNOWN"


class HealthState(StrEnum):
    """Lightweight provider/model health, derived from real requests."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


#: Errors worth retrying against the SAME model (bounded).
RETRYABLE_KINDS = frozenset(
    {
        ErrorKind.RATE_LIMITED,
        ErrorKind.TIMEOUT,
        ErrorKind.SERVER_ERROR,
        ErrorKind.PROVIDER_UNAVAILABLE,
        ErrorKind.UNKNOWN,
    }
)

#: Errors a retry can never fix: recover by changing the model or the request.
NON_RETRYABLE_KINDS = frozenset(
    {
        ErrorKind.AUTH_FAILED,
        ErrorKind.MODEL_NOT_FOUND,
        ErrorKind.INVALID_REQUEST,
        ErrorKind.TOOL_UNSUPPORTED,
        ErrorKind.CONTEXT_TOO_LARGE,
        ErrorKind.QUOTA_EXHAUSTED,
    }
)

#: Errors that mean "this model is not usable right now", so it is dropped
#: from selection until a real success (or a process restart) clears it.
#: Unlike a timeout these are not blips — they are facts about the model or
#: the account's access to it, and re-selecting them would just burn calls.
MODEL_UNAVAILABLE_KINDS = frozenset(
    {
        ErrorKind.AUTH_FAILED,
        ErrorKind.MODEL_NOT_FOUND,
        ErrorKind.QUOTA_EXHAUSTED,
    }
)

#: Which role each task family needs.
ROLE_FOR_TASK: dict[TaskFamily, ModelRole] = {
    TaskFamily.CHAT: ModelRole.FAST,
    TaskFamily.CODING: ModelRole.CODING,
    TaskFamily.REPOSITORY_WORK: ModelRole.CODING,
    TaskFamily.REASONING: ModelRole.REASONING,
    TaskFamily.RESEARCH: ModelRole.TOOL_USE,
    TaskFamily.TOOL_HEAVY: ModelRole.TOOL_USE,
    TaskFamily.LONG_CONTEXT: ModelRole.LONG_CONTEXT,
    TaskFamily.SIMPLE_TRANSFORMATION: ModelRole.FAST,
}

#: Which task families require a model that can actually call tools.
REQUIRES_TOOLS: dict[TaskFamily, bool] = {
    TaskFamily.CHAT: False,
    TaskFamily.CODING: True,
    TaskFamily.REPOSITORY_WORK: True,
    TaskFamily.REASONING: False,
    TaskFamily.RESEARCH: True,
    TaskFamily.TOOL_HEAVY: True,
    TaskFamily.LONG_CONTEXT: False,
    TaskFamily.SIMPLE_TRANSFORMATION: False,
}

#: Context size a LONG_CONTEXT task *aims* for. It is a preference, not a
#: hard filter: when no available model reaches it, the largest available
#: model is chosen for the role instead of failing the task. Callers that
#: genuinely need a floor pass ``min_context=`` explicitly, and that IS
#: enforced.
LARGE_CONTEXT_TOKENS = 200_000


# ---------------------------------------------------------------------------
# Task classification (deterministic, no LLM in the loop)
# ---------------------------------------------------------------------------

_REPOSITORY_PATTERNS = [
    r"\brepo\b",
    r"\brepository\b",
    r"\bcodebase\b",
    r"\bcode\s*base\b",
    r"\bgit\b",
    r"\bcommit(s|ted)?\b",
    r"\bpull\s*request\b",
    r"\bmerge\b",
    r"\bbranch(es)?\b",
    r"\bmonorepo\b",
    r"\bworking\s+tree\b",
]

#: Anchored research openers. They precede the shared cheap classifier because
#: "search the web for <library>" otherwise classifies by whatever the library
#: happens to be named (e.g. "pytest" looked like coding).
_RESEARCH_HINTS = [
    r"^search\s+(the\s+)?(web|google|internet|online)\b",
    r"^look\s+up\b",
    r"^browse\s+(the\s+)?(web|internet)\b",
    r"^research\b",
    r"^find\s+(info|information|out)\b",
]

_REASONING_PATTERNS = [
    r"\bwhy\b",
    r"\bprove\b",
    r"\bderive\b",
    r"\breason(ing|s)?\b",
    r"\btrade[- ]?offs?\b",
    r"\barchitecture\b",
    r"\bdesign\s+(a|the|an)\b",
    r"\bcompare\b",
    r"\bevaluate\b",
    r"\banaly[sz]e\b",
    r"\bstep[- ]by[- ]step\b",
    r"\bthink\s+(through|about)\b",
]

_LONG_CONTEXT_PATTERNS = [
    r"\bentire\s+(file|codebase|document|log|repo|transcript)\b",
    r"\bwhole\s+(file|codebase|document|log|repo)\b",
    r"\bfull\s+(file|document|log|transcript)\b",
    r"\blong\s+(file|document|log|transcript)\b",
    r"\blarge\s+(file|document|codebase)\b",
    r"\bsummar(y|ise|ize)\s+(this|the)\s+(document|transcript|log|file)\b",
    r"\bread\s+(all|every)\b",
    r"\b[0-9]{4,}\s*lines\b",
]

_SIMPLE_TRANSFORMATION_PATTERNS = [
    r"^\s*(convert|transform|translate|reformat|format|minify|prettify|rename)\b",
    r"\bjson\s+to\s+(yaml|toml|csv)\b",
    r"\b(yaml|toml|csv)\s+to\s+json\b",
    r"\buppercase\b",
    r"\blowercase\b",
    r"\bto\s+snake[- ]?case\b",
    r"\bto\s+camel[- ]?case\b",
]


def _matches(patterns: list[str], text: str) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def classify_task(text: str) -> TaskFamily:
    """Classify a request into a :class:`TaskFamily` (deterministic).

    Precedence is fixed and inspectable: long-context signals, then
    repository work, then simple transformations, then reasoning, then the
    shared cheap classifier (``services/classifier.py``) for the remaining
    chat / coding / research / tool / long-running buckets. No model is ever
    asked to classify another model's task.
    """
    from agent_system.services.classifier import classify_request_type

    raw = (text or "").strip()
    if not raw:
        return TaskFamily.CHAT
    low = raw.lower()

    if _matches(_LONG_CONTEXT_PATTERNS, low):
        return TaskFamily.LONG_CONTEXT
    if _matches(_REPOSITORY_PATTERNS, low):
        return TaskFamily.REPOSITORY_WORK
    if _matches(_SIMPLE_TRANSFORMATION_PATTERNS, low):
        return TaskFamily.SIMPLE_TRANSFORMATION
    if _matches(_REASONING_PATTERNS, low):
        return TaskFamily.REASONING
    if _matches(_RESEARCH_HINTS, low):
        return TaskFamily.RESEARCH

    base = classify_request_type(raw)
    return {
        "CODING_TASK": TaskFamily.CODING,
        "RESEARCH_TASK": TaskFamily.RESEARCH,
        "LONG_RUNNING_TASK": TaskFamily.LONG_CONTEXT,
        "TOOL_TASK": TaskFamily.TOOL_HEAVY,
    }.get(base, TaskFamily.CHAT)


# ---------------------------------------------------------------------------
# Error normalization
# ---------------------------------------------------------------------------

_CONTEXT_OVERFLOW_MARKERS = (
    "context length",
    "context_length",
    "context window",
    "maximum context",
    "max context",
    "too many tokens",
    "prompt is too long",
    "reduce the length",
    "exceeds the context",
    "input is too long",
)

_UNAVAILABLE_MARKERS = (
    "no adapter registered",
    "circuit is open",
    "circuit is half-open",
    "providerunavailable",
    "unavailable",
    "unreachable",
)

#: Plan/access refusals. Retrying these is pointless; the fix is another
#: model. ``payment required`` is the HTTP 402 reason phrase Ollama Cloud
#: returns for models outside the account's plan.
_QUOTA_MARKERS = (
    "payment required",
    "requires a subscription",
    "no active subscription",
    "insufficient_quota",
    "quota exceeded",
    "exceeded your current quota",
    "billing",
    "upgrade your plan",
)

#: A bare 402 status code, without matching an unrelated number in a body.
_STATUS_402_RE = re.compile(r"\b402\b")


def normalize_error(error: str | None) -> ErrorKind:
    """Map any adapter/router error string to one :class:`ErrorKind`.

    Delegates to the shared structured classifier
    (``services/llm_contract.py``) so the router, health tracker and fallback
    ledger keep one taxonomy; context overflow and "provider unreachable"
    are detected first because they are reported as generic 400/500 texts.
    """
    text = (error or "").strip()
    if not text:
        return ErrorKind.UNKNOWN
    low = text.lower()

    # Plan/access refusals (HTTP 402 and its wordings) are checked before the
    # generic status handling: they must never be retried, and they are not
    # "provider down" either — only this model is out of reach.
    if any(marker in low for marker in _QUOTA_MARKERS) or (
        "status" in low and _STATUS_402_RE.search(low)
    ):
        return ErrorKind.QUOTA_EXHAUSTED
    if any(marker in low for marker in _CONTEXT_OVERFLOW_MARKERS):
        return ErrorKind.CONTEXT_TOO_LARGE
    if any(marker in low for marker in _UNAVAILABLE_MARKERS):
        return ErrorKind.PROVIDER_UNAVAILABLE

    try:
        from agent_system.services.llm_contract import (
            ProviderErrorCode,
            classify_provider_error,
        )

        code = classify_provider_error(text).code
    except Exception:  # pragma: no cover - contract module is always present
        return ErrorKind.UNKNOWN
    return {
        ProviderErrorCode.AUTHENTICATION: ErrorKind.AUTH_FAILED,
        ProviderErrorCode.INVALID_MODEL: ErrorKind.MODEL_NOT_FOUND,
        ProviderErrorCode.MODEL_UNAVAILABLE: ErrorKind.MODEL_NOT_FOUND,
        ProviderErrorCode.RATE_LIMITED: ErrorKind.RATE_LIMITED,
        ProviderErrorCode.QUOTA_EXHAUSTED: ErrorKind.QUOTA_EXHAUSTED,
        ProviderErrorCode.TIMEOUT: ErrorKind.TIMEOUT,
        ProviderErrorCode.NETWORK: ErrorKind.PROVIDER_UNAVAILABLE,
        ProviderErrorCode.SERVER_ERROR: ErrorKind.SERVER_ERROR,
        ProviderErrorCode.TOOL_UNSUPPORTED: ErrorKind.TOOL_UNSUPPORTED,
        ProviderErrorCode.STRUCTURED_OUTPUT_UNSUPPORTED: ErrorKind.TOOL_UNSUPPORTED,
        ProviderErrorCode.CONTENT_POLICY: ErrorKind.INVALID_REQUEST,
        ProviderErrorCode.INVALID_REQUEST: ErrorKind.INVALID_REQUEST,
        ProviderErrorCode.UNKNOWN: ErrorKind.UNKNOWN,
    }.get(code, ErrorKind.UNKNOWN)


def is_retryable(kind: ErrorKind) -> bool:
    """True when retrying the same model/request could plausibly succeed."""
    return kind in RETRYABLE_KINDS


# ---------------------------------------------------------------------------
# Health tracking (real requests are the signal)
# ---------------------------------------------------------------------------


@dataclass
class ModelHealth:
    provider: str
    model_id: str
    state: HealthState = HealthState.HEALTHY
    consecutive_failures: int = 0
    timeouts: int = 0
    last_success: float | None = None
    last_failure: float | None = None
    last_error: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "state": self.state.value,
            "consecutive_failures": self.consecutive_failures,
            "timeouts": self.timeouts,
            "last_success": self.last_success,
            "last_failure": self.last_failure,
            "last_error": self.last_error[:200],
        }


class InferenceHealthTracker:
    """Thread-safe per provider/model health derived from real calls only.

    No periodic API health checks: every genuine inference request reports an
    outcome, so an idle system performs zero health traffic. States:

    - ``HEALTHY`` — the last request succeeded.
    - ``DEGRADED`` — one transient failure since the last success.
    - ``TEMPORARILY_UNAVAILABLE`` — repeated transient failures.
    - ``UNAVAILABLE`` — a hard failure (bad key, missing model).
    """

    TEMPORARY_THRESHOLD = 2

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._lock = threading.Lock()
        self._status: dict[str, ModelHealth] = {}
        self._clock = clock or time.monotonic

    @staticmethod
    def _key(provider: str, model_id: str) -> str:
        return f"{provider or ''}/{model_id or ''}"

    def get(self, provider: str, model_id: str = "") -> ModelHealth:
        with self._lock:
            key = self._key(provider, model_id)
            entry = self._status.get(key)
            if entry is None:
                entry = ModelHealth(provider=provider, model_id=model_id)
                self._status[key] = entry
            return entry

    def report_success(self, provider: str, model_id: str = "") -> None:
        with self._lock:
            key = self._key(provider, model_id)
            entry = self._status.get(key) or ModelHealth(provider=provider, model_id=model_id)
            entry.state = HealthState.HEALTHY
            entry.consecutive_failures = 0
            entry.last_error = ""
            entry.last_success = self._clock()
            self._status[key] = entry

    def report_failure(
        self, provider: str, model_id: str, kind: ErrorKind, error: str = ""
    ) -> ModelHealth:
        with self._lock:
            key = self._key(provider, model_id)
            entry = self._status.get(key) or ModelHealth(provider=provider, model_id=model_id)
            entry.consecutive_failures += 1
            entry.last_failure = self._clock()
            entry.last_error = str(error)[:200]
            if kind == ErrorKind.TIMEOUT:
                entry.timeouts += 1
            if kind in MODEL_UNAVAILABLE_KINDS:
                entry.state = HealthState.UNAVAILABLE
            elif entry.consecutive_failures >= self.TEMPORARY_THRESHOLD:
                entry.state = HealthState.TEMPORARILY_UNAVAILABLE
            else:
                entry.state = HealthState.DEGRADED
            self._status[key] = entry
            return entry

    def state_for(self, provider: str, model_id: str = "") -> HealthState:
        return self.get(provider, model_id).state

    def is_available(self, provider: str, model_id: str = "") -> bool:
        """False only for genuinely unavailable models (hard failures)."""
        return self.state_for(provider, model_id) != HealthState.UNAVAILABLE

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [entry.to_json() for entry in self._status.values()]

    def reset(self) -> None:
        """Forget every observation (test isolation / explicit operator reset)."""
        with self._lock:
            self._status.clear()


#: Process-wide inference health (real requests only).
INFERENCE_HEALTH = InferenceHealthTracker()


# ---------------------------------------------------------------------------
# Session model lock
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSelection:
    """The decided provider/model for one task, with its full rationale."""

    provider: str
    model_id: str
    role: ModelRole
    task: TaskFamily
    reason: str
    #: Ordered full chain: primary model first, compatible Ollama Cloud
    #: alternates next, emergency providers last. Never contains a model
    #: that cannot satisfy the task's required capabilities.
    candidates: tuple[tuple[str, str], ...] = ()
    is_primary: bool = True
    locked: bool = False

    @property
    def is_ollama_cloud(self) -> bool:
        return self.provider == PRIMARY_PROVIDER

    @property
    def emergency_candidates(self) -> tuple[tuple[str, str], ...]:
        return tuple(c for c in self.candidates if c[0] != PRIMARY_PROVIDER)

    def to_json(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "role": self.role.value,
            "task": self.task.value,
            "reason": self.reason,
            "candidates": [f"{p}:{m}" for p, m in self.candidates],
            "is_primary": self.is_primary,
            "locked": self.locked,
        }


def describe_selection(selection: ModelSelection) -> str:
    """Compact, credential-free status line for Telegram.

    ``⚙️ Ollama Cloud · <model>`` — never an internal id, database key or
    API detail.
    """
    spec = provider_spec(selection.provider)
    label = spec.label if spec is not None else selection.provider
    return f"⚙️ {label} · {selection.model_id}"


class SessionModelLock:
    """In-process session -> selection lock (one model per session).

    Keyed by session (falling back to the task id when there is no session),
    so every inference call inside a session reuses the SAME model instead of
    re-rolling one. Cleared per session, and process-wide between test runs.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._locks: dict[str, ModelSelection] = {}

    def get(self, key: str | None) -> ModelSelection | None:
        if not key:
            return None
        with self._lock:
            return self._locks.get(key)

    def set(self, key: str | None, selection: ModelSelection) -> None:
        if not key:
            return
        with self._lock:
            self._locks[key] = selection

    def clear(self, key: str | None) -> None:
        if not key:
            return
        with self._lock:
            self._locks.pop(key, None)

    def clear_all(self) -> None:
        with self._lock:
            self._locks.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._locks)


#: Process-wide session model locks.
INFERENCE_LOCKS = SessionModelLock()


def lock_key(session_id: str | None, task_id: str | None = None) -> str | None:
    """Session-scoped lock key (task id fallback for session-less callers)."""
    if session_id:
        return f"session:{session_id}"
    if task_id:
        return f"task:{task_id}"
    return None


# ---------------------------------------------------------------------------
# Model role resolution + validation
# ---------------------------------------------------------------------------

ROLE_SETTINGS_ATTR: dict[ModelRole, str] = {
    ModelRole.GENERAL: "ollama_general_model",
    ModelRole.FAST: "ollama_fast_model",
    ModelRole.CODING: "ollama_coding_model",
    ModelRole.REASONING: "ollama_reasoning_model",
    ModelRole.LONG_CONTEXT: "ollama_long_context_model",
    ModelRole.TOOL_USE: "ollama_tool_model",
}


def _ollama_capabilities(catalog: CapabilityCatalog) -> list[ModelCapability]:
    return [m for m in catalog.all() if m.provider == PRIMARY_PROVIDER]


def _role_score(cap: ModelCapability, role: ModelRole) -> tuple[int, int, int, str]:
    """Deterministic preference score for a role (lower sorts first)."""
    latency = {"fast": 0, "standard": 1, "slow": 2}.get(cap.latency_class, 1)
    if role == ModelRole.FAST:
        return (latency, -int(cap.coding), -cap.context_limit, cap.model_id)
    if role == ModelRole.CODING:
        return (-int(cap.coding), latency, -cap.context_limit, cap.model_id)
    if role == ModelRole.REASONING:
        return (
            0 if cap.effective_supports_reasoning else 1,
            -int(cap.coding),
            latency,
            cap.model_id,
        )
    if role == ModelRole.LONG_CONTEXT:
        return (-cap.context_limit, latency, -int(cap.coding), cap.model_id)
    if role == ModelRole.TOOL_USE:
        return (
            0 if cap.supports_parallel_tools else (1 if cap.effective_supports_tools else 2),
            latency,
            -cap.context_limit,
            cap.model_id,
        )
    return (latency, 0 if cap.effective_supports_tools else 1, -cap.context_limit, cap.model_id)


def _satisfies(
    cap: ModelCapability,
    *,
    requires_tools: bool,
    requires_structured: bool,
    min_context: int,
) -> bool:
    if requires_tools and not cap.effective_supports_tools:
        return False
    if requires_structured and not cap.effective_supports_structured:
        return False
    return cap.context_limit >= max(0, int(min_context or 0))


def resolve_role_models(
    settings: Any,
    catalog: CapabilityCatalog | None = None,
    *,
    requires_tools: bool = False,
    min_context: int = 0,
    health: InferenceHealthTracker | None = None,
) -> dict[ModelRole, tuple[str, str]]:
    """Resolve each role to ``(model_id, source)`` for Ollama Cloud.

    Configured role models win (``OLLAMA_*_MODEL``); an unconfigured role is
    *discovered* from the capability catalog instead of being invented. A
    configured model that the catalog does not know, that cannot satisfy the
    task's required capabilities, or that is marked UNAVAILABLE by real
    requests (bad key, missing model, HTTP 402 outside the plan) is skipped —
    never silently substituted by an incompatible model.

    Filtering by ``health`` is what makes a plan refusal self-healing: the
    first request teaches the runtime that a model is out of reach, and every
    later task skips it instead of re-learning it.

    Returns an empty mapping when no Ollama Cloud model is usable at all,
    which is the signal for the caller to use the emergency layer.
    """
    catalog = catalog or DEFAULT_CATALOG
    health = health or INFERENCE_HEALTH
    configured = dict(getattr(settings, "ollama_model_roles", {}) or {})
    caps = [
        cap
        for cap in _ollama_capabilities(catalog)
        if health.is_available(PRIMARY_PROVIDER, cap.model_id)
    ]
    resolved: dict[ModelRole, tuple[str, str]] = {}

    for role in ModelRole:
        attr_model = configured.get(role.value.lower())
        if attr_model:
            cap = catalog.get(PRIMARY_PROVIDER, attr_model)
            if cap is None:
                logger.warning(
                    "inference.role_model_unknown role=%s model=%s", role.value, attr_model
                )
            elif not health.is_available(PRIMARY_PROVIDER, attr_model):
                logger.info(
                    "inference.role_model_unavailable role=%s model=%s",
                    role.value,
                    attr_model,
                )
            elif _satisfies(
                cap,
                requires_tools=requires_tools,
                requires_structured=False,
                min_context=min_context,
            ):
                resolved[role] = (attr_model, "configured")
                continue
            else:
                logger.info(
                    "inference.role_capability_mismatch role=%s model=%s",
                    role.value,
                    attr_model,
                )
        compatible = [
            c
            for c in caps
            if _satisfies(
                c,
                requires_tools=requires_tools,
                requires_structured=False,
                min_context=min_context,
            )
        ]
        if not compatible:
            continue
        best = min(compatible, key=lambda c: _role_score(c, role))
        resolved[role] = (best.model_id, "discovered")
    return resolved


def validate_configuration(
    settings: Any, catalog: CapabilityCatalog | None = None
) -> dict[str, Any]:
    """Startup diagnostics: credentials, model ids, roles, availability.

    Never fails the application because one optional role is unavailable —
    the report says which roles resolved, which are missing, and which
    configured ids are unknown to the capability catalog.
    """
    from agent_system.services.providers import configured_providers

    catalog = catalog or DEFAULT_CATALOG
    roles = resolve_role_models(settings, catalog)
    configured = dict(getattr(settings, "ollama_model_roles", {}) or {})
    unknown = sorted(
        model for model in configured.values() if catalog.get(PRIMARY_PROVIDER, model) is None
    )
    ollama_configured = any(
        p["key"] == PRIMARY_PROVIDER and p["configured"] for p in configured_providers(settings)
    )
    diagnostics: dict[str, Any] = {
        "primary_provider": PRIMARY_PROVIDER,
        "ollama_cloud_configured": bool(ollama_configured),
        "roles": {role.value: model for role, (model, _src) in roles.items()},
        "roles_disabled": [
            role.value for role in ModelRole if role not in roles and role != ModelRole.GENERAL
        ],
        "unknown_configured_models": unknown,
        "capabilities": {
            model: catalog.get(PRIMARY_PROVIDER, model).to_json()  # type: ignore[union-attr]
            for model in sorted({m for m, _ in roles.values()})
            if catalog.get(PRIMARY_PROVIDER, model) is not None
        },
        "emergency_fallback_enabled": bool(getattr(settings, "emergency_fallback_enabled", True)),
        "fallback_providers": list(getattr(settings, "fallback_provider_list", []) or []),
        "streaming": bool(getattr(settings, "ollama_streaming", True)),
        "checkpointing": bool(getattr(settings, "task_checkpointing", True)),
    }
    return diagnostics


# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------


def _provider_configured(provider: str, settings: Any) -> bool:
    """True when the provider can actually be called (credential present)."""
    if not provider:
        return False
    if provider in ("echo", "none"):
        return True
    spec = provider_spec(provider)
    if spec is None:
        return False
    try:
        if provider in ("ollama",):
            return True  # keyless local runtime
        return bool(settings.provider_api_key(provider))
    except Exception:
        return False


def effective_primary(settings: Any) -> tuple[str, str]:
    """Resolve the effective primary provider and why.

    The explicit offline tier stays authoritative (an operator who set
    ``DEFAULT_PROVIDER=echo`` never gets silently promoted onto the network).
    When Ollama Cloud has no credential, the deployment keeps working on its
    configured provider rather than failing — reported, not hidden.
    """
    default_provider = str(getattr(settings, "default_provider", "") or "")
    primary = str(getattr(settings, "primary_provider", "") or "").strip().lower()
    if is_offline_provider(default_provider):
        return default_provider, "offline_authoritative"
    if is_offline_provider(primary) or not primary:
        return default_provider or "echo", "offline_authoritative"
    if primary == PRIMARY_PROVIDER and not _provider_configured(primary, settings):
        return default_provider or PRIMARY_PROVIDER, "ollama_cloud_unconfigured"
    if not _provider_configured(primary, settings):
        return default_provider or primary, "primary_unconfigured"
    return primary, "configured_primary"


def _emergency_candidates(
    settings: Any,
    catalog: CapabilityCatalog,
    *,
    requires_tools: bool,
    requires_structured: bool,
    min_context: int,
    health: InferenceHealthTracker,
    exclude: str,
) -> list[tuple[str, str]]:
    """Capability-compatible emergency candidates, in configured order.

    A provider is skipped when it is not configured, is the failing primary,
    is marked unavailable, or has no catalogued model that satisfies the
    required capabilities — an incompatible model is never substituted.
    """
    if not getattr(settings, "emergency_fallback_enabled", True):
        return []
    out: list[tuple[str, str]] = []
    for provider in getattr(settings, "fallback_provider_list", []) or []:
        if provider == exclude or provider == PRIMARY_PROVIDER:
            continue
        if not _provider_configured(provider, settings):
            continue
        compatible = [
            cap
            for cap in catalog.all()
            if cap.provider == provider
            and _satisfies(
                cap,
                requires_tools=requires_tools,
                requires_structured=requires_structured,
                min_context=min_context,
            )
            and health.is_available(provider, cap.model_id)
        ]
        if not compatible:
            continue
        best = min(compatible, key=lambda c: _role_score(c, ModelRole.GENERAL))
        out.append((provider, best.model_id))
    return out


def _offline_selection(settings: Any, task: TaskFamily, role: ModelRole) -> ModelSelection:
    from agent_system.services.providers import default_model_id

    provider, reason = effective_primary(settings)
    model_id = (
        str(getattr(settings, "default_model", "") or "")
        or default_model_id(settings)
        or "echo-default"
    )
    return ModelSelection(
        provider=provider,
        model_id=model_id,
        role=role,
        task=task,
        reason=reason,
        candidates=((provider, model_id),),
        is_primary=True,
    )


def select_model(
    settings: Any,
    text: str = "",
    *,
    catalog: CapabilityCatalog | None = None,
    health: InferenceHealthTracker | None = None,
    locks: SessionModelLock | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    requires_tools: bool | None = None,
    requires_structured: bool = False,
    min_context: int | None = None,
    role_override: ModelRole | None = None,
    use_lock: bool = True,
) -> ModelSelection:
    """Select the provider/model for a task — deterministic and inspectable.

    Pipeline: effective primary -> task classification -> capability
    requirements -> session lock (reuse) -> role model -> capability
    compatibility -> ordered candidate chain (Ollama Cloud first, then
    compatible Ollama alternates, then emergency providers).
    """
    catalog = catalog or DEFAULT_CATALOG
    health = health or INFERENCE_HEALTH
    locks = locks or INFERENCE_LOCKS

    task = classify_task(text)
    role = role_override or ROLE_FOR_TASK.get(task, ModelRole.GENERAL)
    tool_required = REQUIRES_TOOLS.get(task, False) if requires_tools is None else requires_tools
    # Only a caller-supplied ``min_context`` is a hard floor. A LONG_CONTEXT
    # task *prefers* the largest window (the role score sorts on it) but must
    # not be refused Ollama Cloud entirely when the biggest model is smaller
    # than the aim — that would push long tasks onto the emergency layer for
    # no reason.
    context_required = int(min_context) if min_context is not None else 0

    primary, primary_reason = effective_primary(settings)
    if is_offline_provider(primary):
        return _offline_selection(settings, task, role)

    key = lock_key(session_id, task_id)
    if use_lock:
        locked = locks.get(key)
        if locked is not None and locked.provider == primary:
            cap = catalog.get(locked.provider, locked.model_id)
            if health.is_available(locked.provider, locked.model_id) and (
                cap is None
                or _satisfies(
                    cap,
                    requires_tools=tool_required,
                    requires_structured=requires_structured,
                    min_context=context_required,
                )
            ):
                # Reuse verbatim: no re-selection, no provider switch.
                return ModelSelection(
                    provider=locked.provider,
                    model_id=locked.model_id,
                    role=locked.role,
                    task=task,
                    reason=f"session_lock:{locked.model_id}",
                    candidates=locked.candidates,
                    is_primary=True,
                    locked=True,
                )
            logger.info(
                "inference.lock_released key=%s model=%s (unavailable or incompatible)",
                key,
                locked.model_id,
            )
            locks.clear(key)

    if primary != PRIMARY_PROVIDER:
        # No Ollama Cloud credential: stay on the configured provider and its
        # own model, with compatible fallbacks as the emergency chain.
        spec = provider_spec(primary)
        model_id = str(getattr(settings, "default_model", "") or "") or (
            spec.default_model if spec is not None else "echo-default"
        )
        candidates = [(primary, model_id)]
        candidates.extend(
            _emergency_candidates(
                settings,
                catalog,
                requires_tools=tool_required,
                requires_structured=requires_structured,
                min_context=context_required,
                health=health,
                exclude=primary,
            )
        )
        selection = ModelSelection(
            provider=primary,
            model_id=model_id,
            role=role,
            task=task,
            reason=primary_reason,
            candidates=tuple(candidates),
            is_primary=False,
        )
        if use_lock:
            locks.set(key, selection)
        return selection

    roles = resolve_role_models(
        settings,
        catalog,
        requires_tools=tool_required,
        min_context=context_required,
        health=health,
    )
    used_fallback_role = role not in roles
    chosen = roles.get(role) or roles.get(ModelRole.GENERAL) or roles.get(ModelRole.TOOL_USE)
    if chosen is None:
        # No Ollama Cloud model satisfies the requirement: emergency layer is
        # the only remaining option (reported in the reason).
        emergency = _emergency_candidates(
            settings,
            catalog,
            requires_tools=tool_required,
            requires_structured=requires_structured,
            min_context=context_required,
            health=health,
            exclude=PRIMARY_PROVIDER,
        )
        if not emergency:
            return _offline_selection(settings, task, role)
        provider, model_id = emergency[0]
        selection = ModelSelection(
            provider=provider,
            model_id=model_id,
            role=role,
            task=task,
            reason="no_compatible_ollama_model",
            candidates=tuple(emergency),
            is_primary=False,
        )
        if use_lock:
            locks.set(key, selection)
        return selection

    model_id, source = chosen
    reason = f"role={role.value} source={source} task={task.value}"
    if used_fallback_role:
        reason += f" fallback_role={ModelRole.GENERAL.value}"

    # Compatible Ollama Cloud alternates (prefer another Ollama model before
    # any external provider — §10).
    requires = [
        cap
        for cap in _ollama_capabilities(catalog)
        if cap.model_id != model_id
        and health.is_available(PRIMARY_PROVIDER, cap.model_id)
        and _satisfies(
            cap,
            requires_tools=tool_required,
            requires_structured=requires_structured,
            min_context=context_required,
        )
    ]
    alternates = [
        (PRIMARY_PROVIDER, cap.model_id)
        for cap in sorted(requires, key=lambda c: _role_score(c, role))
    ]
    candidates: list[tuple[str, str]] = [(PRIMARY_PROVIDER, model_id), *alternates]
    candidates.extend(
        _emergency_candidates(
            settings,
            catalog,
            requires_tools=tool_required,
            requires_structured=requires_structured,
            min_context=context_required,
            health=health,
            exclude=PRIMARY_PROVIDER,
        )
    )
    selection = ModelSelection(
        provider=PRIMARY_PROVIDER,
        model_id=model_id,
        role=role,
        task=task,
        reason=reason,
        candidates=tuple(candidates),
        is_primary=True,
    )
    if use_lock:
        locks.set(key, selection)
    logger.info(
        "inference.model_selected task=%s role=%s model=%s reason=%s",
        task.value,
        role.value,
        model_id,
        reason,
    )
    return selection


# ---------------------------------------------------------------------------
# Bounded retry + emergency fallback execution
# ---------------------------------------------------------------------------


@dataclass
class RuntimeInvocation:
    """Result of one runtime-governed inference call."""

    result: Any
    selection: ModelSelection
    fallback_used: bool = False
    attempts: list[dict[str, Any]] = field(default_factory=list)
    error_kind: ErrorKind | None = None
    notice: str | None = None
    checkpoint_id: str | None = None

    @property
    def ok(self) -> bool:
        return bool(getattr(self.result, "ok", False))

    @property
    def output(self) -> str:
        return str(getattr(self.result, "output", "") or "")


def retry_delays(
    settings: Any,
    attempts: int,
    *,
    first_kind: ErrorKind | None = None,
) -> list[float]:
    """Exponential backoff delays for ``attempts`` total tries (never busy).

    ``attempts`` is the TOTAL number of tries, so ``attempts - 1`` waits are
    produced. Rate-limit errors wait 4x longer, matching the documented
    philosophy of honouring the provider's own pacing.
    """
    base = float(getattr(settings, "ollama_retry_base_delay", 1.0) or 1.0)
    cap = float(getattr(settings, "ollama_retry_max_delay", DEFAULT_MAX_DELAY) or DEFAULT_MAX_DELAY)
    multiplier = 4.0 if first_kind == ErrorKind.RATE_LIMITED else 1.0
    return [min(base * multiplier * (2**index), cap) for index in range(max(0, int(attempts) - 1))]


def _call_once(
    router: Any,
    factory: Any,
    model_id: str,
    prompt: str,
    *,
    session_id: str | None,
    task_id: str | None,
    agent_run_id: str | None,
    agent_type: str,
    stream: bool,
    on_token: Any | None,
    timeout: float | None = None,
) -> Any:
    """One adapter call through the router (streaming when supported).

    Token events are status-only for Telegram (the presenter maps them to
    "✍️ Writing the reply..."), so a retry after a partial stream is safe:
    the user-visible answer is still the single final ``task.completed``.
    """
    kwargs: dict[str, Any] = {
        "session_id": session_id,
        "task_id": task_id,
        "agent_run_id": agent_run_id,
        "agent_type": agent_type,
    }
    if stream and hasattr(router, "invoke_streaming"):
        return router.invoke_streaming(factory, model_id, prompt, on_token=on_token, **kwargs)
    # ``timeout`` is a per-call override owned by ModelRouter.invoke (it bounds
    # one adapter call); it is not a streaming parameter, so it is only sent on
    # the non-streaming path.
    if timeout is not None:
        kwargs["timeout"] = float(timeout)
    return router.invoke(factory, model_id, prompt, **kwargs)


def invoke(
    router: Any,
    factory: Any,
    settings: Any,
    prompt: str,
    *,
    text: str = "",
    session_id: str | None = None,
    task_id: str | None = None,
    agent_run_id: str | None = None,
    agent_type: str = "llm",
    on_token: Any | None = None,
    stream: bool | None = None,
    timeout: float | None = None,
    emit: Any | None = None,
    requires_tools: bool | None = None,
    requires_structured: bool = False,
    min_context: int | None = None,
    catalog: CapabilityCatalog | None = None,
    health: InferenceHealthTracker | None = None,
    locks: SessionModelLock | None = None,
    sleeper: Callable[[float], None] | None = None,
    selection: ModelSelection | None = None,
    checkpoint_payload: dict[str, Any] | None = None,
    reserve: bool = False,
) -> RuntimeInvocation:
    """Run one inference call under the Ollama Cloud-first policy.

    ``reserve=True`` selects the model + locks the session and records the
    resume checkpoint WITHOUT calling a provider — used when the caller
    cannot make the call yet (e.g. its own retry loop owns the call).
    """
    catalog = catalog or DEFAULT_CATALOG
    health = health or INFERENCE_HEALTH
    locks = locks or INFERENCE_LOCKS
    sleep = sleeper or time.sleep

    if selection is None:
        selection = select_model(
            settings,
            text or prompt,
            catalog=catalog,
            health=health,
            locks=locks,
            session_id=session_id,
            task_id=task_id,
            requires_tools=requires_tools,
            requires_structured=requires_structured,
            min_context=min_context,
        )
    if reserve:
        return RuntimeInvocation(
            result=None,
            selection=selection,
            notice=None,
            checkpoint_id=_maybe_checkpoint(
                settings,
                factory,
                selection,
                session_id=session_id,
                task_id=task_id,
                kind=ErrorKind.UNKNOWN,
                payload=checkpoint_payload,
            ),
        )

    stream_on = (
        bool(getattr(settings, "ollama_streaming", True)) if stream is None else bool(stream)
    )
    use_stream = stream_on and selection.is_primary
    attempts = max(1, int(getattr(settings, "ollama_retry_attempts", 3) or 3))

    history: list[dict[str, Any]] = []
    last_result: Any = None
    last_kind: ErrorKind | None = None
    primary_attempts = attempts if selection.is_primary else 1

    for attempt_no in range(1, primary_attempts + 1):
        result = _call_once(
            router,
            factory,
            selection.model_id,
            prompt,
            session_id=session_id,
            task_id=task_id,
            agent_run_id=agent_run_id,
            agent_type=agent_type,
            stream=use_stream,
            on_token=on_token,
            timeout=timeout,
        )
        last_result = result
        if bool(getattr(result, "ok", False)) and str(getattr(result, "output", "") or "").strip():
            health.report_success(selection.provider, selection.model_id)
            history.append(
                {"provider": selection.provider, "model_id": selection.model_id, "ok": True}
            )
            _persist_lock(factory, selection, session_id=session_id, task_id=task_id)
            _emit(
                emit,
                "inference.model_ok",
                {
                    "provider": selection.provider,
                    "model_id": selection.model_id,
                    "attempt": attempt_no,
                    "fallback": not selection.is_primary,
                },
            )
            if not selection.is_primary:
                _mark_resumed(factory, task_id, selection)
            return RuntimeInvocation(
                result=result,
                selection=selection,
                fallback_used=not selection.is_primary,
                attempts=history,
                error_kind=None,
            )

        kind = normalize_error(str(getattr(result, "error", "") or "model invocation failed"))
        last_kind = kind
        health.report_failure(
            selection.provider,
            selection.model_id,
            kind,
            error=str(getattr(result, "error", "") or ""),
        )
        history.append(
            {
                "provider": selection.provider,
                "model_id": selection.model_id,
                "ok": False,
                "error_kind": kind.value,
                "attempt": attempt_no,
            }
        )
        if not is_retryable(kind) or attempt_no >= primary_attempts:
            break
        delay = _delay_for(settings, kind, attempt_no, selection.provider, selection.model_id)
        _emit(
            emit,
            "inference.retry",
            {
                "provider": selection.provider,
                "model_id": selection.model_id,
                "attempt": attempt_no + 1,
                "error_kind": kind.value,
                "delay_seconds": delay,
            },
        )
        if delay > 0:
            sleep(delay)

    # Primary exhausted. A capability mismatch (missing model, tool-less
    # model) continues on another COMPATIBLE Ollama Cloud model first (§10);
    # an outright outage goes straight to the emergency layer (§6/§7). Either
    # way the task keeps its ids, tool state and memory — only the model moves.
    chain = _continue_chain(selection, last_kind)
    if not chain:
        return RuntimeInvocation(
            result=last_result,
            selection=selection,
            attempts=history,
            error_kind=last_kind,
        )

    checkpoint_id = _maybe_checkpoint(
        settings,
        factory,
        selection,
        session_id=session_id,
        task_id=task_id,
        kind=last_kind or ErrorKind.UNKNOWN,
        payload=checkpoint_payload,
    )
    notice: str | None = None
    announced_fallback = False

    for provider, model_id in chain:
        if provider == PRIMARY_PROVIDER:
            _emit(
                emit,
                "inference.model_switched",
                {
                    "from_model": selection.model_id,
                    "to_model": model_id,
                    "error_kind": (last_kind or ErrorKind.UNKNOWN).value,
                },
            )
        elif not announced_fallback:
            # Entering the emergency layer is announced exactly once, only
            # when an external provider is actually about to be used.
            announced_fallback = True
            notice = FALLBACK_NOTICE
            _emit(
                emit,
                "inference.fallback_activated",
                {
                    "from_provider": selection.provider,
                    "from_model": selection.model_id,
                    "to_provider": provider,
                    "to_model": model_id,
                    "error_kind": (last_kind or ErrorKind.UNKNOWN).value,
                    "checkpoint_id": checkpoint_id,
                },
            )
        fallback_selection = ModelSelection(
            provider=provider,
            model_id=model_id,
            role=selection.role,
            task=selection.task,
            reason=f"continued_from={selection.model_id}",
            candidates=tuple(chain),
            is_primary=provider == PRIMARY_PROVIDER,
        )
        result = _call_once(
            router,
            factory,
            model_id,
            prompt,
            session_id=session_id,
            task_id=task_id,
            agent_run_id=agent_run_id,
            agent_type=agent_type,
            stream=use_stream and provider == PRIMARY_PROVIDER,
            on_token=on_token,
            timeout=timeout,
        )
        if bool(getattr(result, "ok", False)) and str(getattr(result, "output", "") or "").strip():
            health.report_success(provider, model_id)
            history.append({"provider": provider, "model_id": model_id, "ok": True})
            # The lock follows the task onto the fallback for THIS session
            # only; the next task re-selects Ollama Cloud (never sticky).
            locks.set(lock_key(session_id, task_id), fallback_selection)
            _persist_lock(factory, fallback_selection, session_id=session_id, task_id=task_id)
            _mark_resumed(factory, task_id, fallback_selection)
            emergency_used = provider != PRIMARY_PROVIDER
            return RuntimeInvocation(
                result=result,
                selection=fallback_selection,
                fallback_used=emergency_used,
                attempts=history,
                error_kind=None,
                notice=FALLBACK_NOTICE if emergency_used else None,
                checkpoint_id=checkpoint_id,
            )
        kind = normalize_error(str(getattr(result, "error", "") or "model invocation failed"))
        health.report_failure(
            provider, model_id, kind, error=str(getattr(result, "error", "") or "")
        )
        history.append(
            {
                "provider": provider,
                "model_id": model_id,
                "ok": False,
                "error_kind": kind.value,
            }
        )
        last_result = result
        last_kind = kind

    _emit(
        emit,
        "inference.exhausted",
        {
            "attempts": len(history),
            "error_kind": (last_kind or ErrorKind.UNKNOWN).value,
        },
    )
    return RuntimeInvocation(
        result=last_result,
        selection=selection,
        fallback_used=True,
        attempts=history,
        error_kind=last_kind,
        notice=notice,
        checkpoint_id=checkpoint_id,
    )


def _continue_chain(
    selection: ModelSelection, last_kind: ErrorKind | None
) -> list[tuple[str, str]]:
    """Where to continue after the locked model exhausted its retries.

    Capability failures (missing model, model without tool support) try a
    compatible Ollama Cloud alternate FIRST; every other failure leaves
    Ollama Cloud for the emergency layer. Candidates come from the
    selection's own chain, so an incompatible model can never appear here.
    """
    primary = (selection.provider, selection.model_id)
    others = [c for c in selection.candidates if c != primary]
    if last_kind in (
        ErrorKind.MODEL_NOT_FOUND,
        ErrorKind.TOOL_UNSUPPORTED,
        # A model outside the plan (402) is just as unselectable as a missing
        # one: prefer another OLLAMA CLOUD model before any external provider.
        ErrorKind.QUOTA_EXHAUSTED,
    ):
        ollama = [c for c in others if c[0] == PRIMARY_PROVIDER]
        emergency = [c for c in others if c[0] != PRIMARY_PROVIDER]
        return ollama + emergency
    return [c for c in others if c[0] != selection.provider]


def _delay_for(
    settings: Any,
    kind: ErrorKind,
    attempt_no: int,
    provider: str = "",
    model_id: str = "",
) -> float:
    """Backoff for the next try, honouring a provider rate-limit cooldown."""
    base = float(getattr(settings, "ollama_retry_base_delay", 1.0) or 1.0)
    cap = float(getattr(settings, "ollama_retry_max_delay", DEFAULT_MAX_DELAY) or DEFAULT_MAX_DELAY)
    multiplier = 4.0 if kind == ErrorKind.RATE_LIMITED else 1.0
    delay = min(base * multiplier * (2 ** max(0, attempt_no - 1)), cap)
    try:
        # Respect provider-supplied retry information when it is live: the
        # health tracker stores the Retry-After cooldown from the last 429.
        cooldown = float(GLOBAL_HEALTH_TRACKER.cooldown_remaining(provider, model_id))
        if cooldown > 0:
            delay = max(delay, min(cooldown, cap))
    except Exception:
        pass
    return delay


def _maybe_checkpoint(
    settings: Any,
    factory: Any,
    selection: ModelSelection,
    *,
    session_id: str | None,
    task_id: str | None,
    kind: ErrorKind,
    payload: dict[str, Any] | None,
) -> str | None:
    """Persist a task checkpoint before continuing elsewhere (§14)."""
    if not getattr(settings, "task_checkpointing", True):
        return None
    if factory is None or not task_id:
        return None
    from agent_system.services.checkpoints import build_checkpoint_payload, save_checkpoint

    checkpoint_payload = dict(
        payload
        or build_checkpoint_payload(
            task_state="running",
            context_ref={"session_id": session_id, "task_id": task_id},
            pending_step="inference",
            execution={"role": selection.role.value, "task": selection.task.value},
        )
    )
    # The checkpoint must always name the provider/model it was written under,
    # even when the caller supplied its own payload.
    if not checkpoint_payload.get("active_provider"):
        checkpoint_payload["active_provider"] = selection.provider
    if not checkpoint_payload.get("active_model"):
        checkpoint_payload["active_model"] = selection.model_id
    if not checkpoint_payload.get("task_state"):
        checkpoint_payload["task_state"] = "running"
    return save_checkpoint(
        factory,
        task_id=task_id,
        session_id=session_id,
        provider=selection.provider,
        model_id=selection.model_id,
        error_kind=kind.value,
        payload=checkpoint_payload,
    )


def _mark_resumed(factory: Any, task_id: str | None, selection: ModelSelection) -> None:
    if factory is None or not task_id:
        return
    try:
        from agent_system.services.checkpoints import mark_checkpoint_resumed

        mark_checkpoint_resumed(
            factory, task_id, provider=selection.provider, model_id=selection.model_id
        )
    except Exception:  # pragma: no cover - resume marking is best-effort
        pass


def _persist_lock(
    factory: Any,
    selection: ModelSelection,
    *,
    session_id: str | None,
    task_id: str | None,
) -> None:
    """Durably record the session model lock (idempotent, best-effort)."""
    if factory is None or not session_id:
        return
    try:
        from agent_system.domain import ids
        from agent_system.domain.events import utcnow
        from agent_system.infra.db import session_scope
        from agent_system.infra.models import InferenceModelLock

        now = utcnow()
        with session_scope(factory) as db:
            row = (
                db.query(InferenceModelLock)
                .filter(InferenceModelLock.session_id == str(session_id))
                .one_or_none()
            )
            if row is None:
                row = InferenceModelLock(
                    id=ids.new_id("lock"),
                    session_id=str(session_id),
                    created_at=now,
                )
                db.add(row)
            row.task_id = str(task_id) if task_id else row.task_id
            row.provider = selection.provider
            row.model_id = selection.model_id
            row.role = selection.role.value
            row.task_family = selection.task.value
            row.reason = selection.reason
            row.updated_at = now
    except Exception as exc:  # pragma: no cover - lock persistence is best-effort
        logger.warning("inference.lock_persist_failed session_id=%s error=%s", session_id, exc)


def _emit(emit: Any, event_type: str, payload: dict[str, Any]) -> None:
    if emit is None:
        return
    try:
        emit(event_type, payload)
    except Exception:  # pragma: no cover - emission must never break inference
        pass


__all__ = [
    "DEFAULT_MAX_DELAY",
    "FALLBACK_NOTICE",
    "INFERENCE_HEALTH",
    "INFERENCE_LOCKS",
    "LARGE_CONTEXT_TOKENS",
    "NON_RETRYABLE_KINDS",
    "PRIMARY_PROVIDER",
    "RETRYABLE_KINDS",
    "ROLE_FOR_TASK",
    "ROLE_SETTINGS_ATTR",
    "ErrorKind",
    "HealthState",
    "InferenceHealthTracker",
    "ModelRole",
    "ModelSelection",
    "RuntimeInvocation",
    "SessionModelLock",
    "TaskFamily",
    "classify_task",
    "describe_selection",
    "effective_primary",
    "invoke",
    "is_retryable",
    "lock_key",
    "normalize_error",
    "resolve_role_models",
    "retry_delays",
    "select_model",
    "validate_configuration",
]
