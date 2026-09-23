"""Capability-aware LLM router (Agentic Runtime v1, additive)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FailureCategory(str, Enum):  # noqa: UP042
    # Structured provider taxonomy (superset of the legacy names — legacy
    # members are kept so existing callers/tests keep working).
    AUTHENTICATION = "AUTHENTICATION"
    AUTH_FAILURE = "AUTH_FAILURE"  # legacy alias of AUTHENTICATION
    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_MODEL = "INVALID_MODEL"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    RATE_LIMIT = "RATE_LIMIT"  # legacy alias of RATE_LIMITED
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    TIMEOUT = "TIMEOUT"
    NETWORK = "NETWORK"
    NETWORK_ERROR = "NETWORK_ERROR"  # legacy alias of NETWORK
    SERVER_ERROR = "SERVER_ERROR"
    TOOL_UNSUPPORTED = "TOOL_UNSUPPORTED"
    STRUCTURED_OUTPUT_UNSUPPORTED = "STRUCTURED_OUTPUT_UNSUPPORTED"
    CONTENT_POLICY = "CONTENT_POLICY"
    NOT_FOUND = "NOT_FOUND"  # legacy: invalid model / missing endpoint
    CAPABILITY_MISMATCH = "CAPABILITY_MISMATCH"
    UNKNOWN = "UNKNOWN"


def _normalize_category(category: FailureCategory) -> FailureCategory:
    aliases = {
        FailureCategory.AUTH_FAILURE: FailureCategory.AUTHENTICATION,
        FailureCategory.RATE_LIMIT: FailureCategory.RATE_LIMITED,
        FailureCategory.NETWORK_ERROR: FailureCategory.NETWORK,
    }
    return aliases.get(category, category)


def classify_error(exc_or_msg: Any, status_code: int | None = None) -> tuple[FailureCategory, bool]:
    """Classify model invocation failure into category and retryability.

    Returns (FailureCategory, is_retryable). Delegates to the structured
    :mod:`llm_contract` classifier so the router, health tracker and
    fallback ledger share one taxonomy; legacy member names are preserved.
    """
    try:
        from agent_system.services.llm_contract import ProviderErrorCode
        from agent_system.services.llm_contract import classify_provider_error as _classify
    except ImportError:
        _classify = None  # type: ignore[assignment]
        ProviderErrorCode = None  # type: ignore[assignment]
    if _classify is not None:
        err = _classify(exc_or_msg, status_code=status_code)
        # Legacy member names preserved for existing callers/tests:
        # AUTH_FAILURE (== AUTHENTICATION), NOT_FOUND (invalid model),
        # RATE_LIMIT (== RATE_LIMITED), NETWORK_ERROR (== NETWORK).
        mapping = {
            ProviderErrorCode.AUTHENTICATION: (FailureCategory.AUTH_FAILURE, False),
            ProviderErrorCode.INVALID_REQUEST: (FailureCategory.INVALID_REQUEST, False),
            ProviderErrorCode.INVALID_MODEL: (FailureCategory.NOT_FOUND, False),
            ProviderErrorCode.MODEL_UNAVAILABLE: (FailureCategory.NOT_FOUND, False),
            ProviderErrorCode.RATE_LIMITED: (FailureCategory.RATE_LIMIT, True),
            ProviderErrorCode.QUOTA_EXHAUSTED: (FailureCategory.RATE_LIMIT, True),
            ProviderErrorCode.TIMEOUT: (FailureCategory.TIMEOUT, True),
            ProviderErrorCode.NETWORK: (FailureCategory.NETWORK_ERROR, True),
            ProviderErrorCode.SERVER_ERROR: (FailureCategory.SERVER_ERROR, True),
            ProviderErrorCode.TOOL_UNSUPPORTED: (FailureCategory.TOOL_UNSUPPORTED, False),
            ProviderErrorCode.STRUCTURED_OUTPUT_UNSUPPORTED: (
                FailureCategory.STRUCTURED_OUTPUT_UNSUPPORTED,
                False,
            ),
            ProviderErrorCode.CONTENT_POLICY: (FailureCategory.CONTENT_POLICY, False),
            ProviderErrorCode.UNKNOWN: (FailureCategory.UNKNOWN, True),
        }
        return mapping.get(err.code, (FailureCategory.UNKNOWN, True))
    # Fallback when llm_contract is unavailable (should never happen).
    msg = str(exc_or_msg or "").lower()
    code = status_code
    response = getattr(exc_or_msg, "response", None)
    if code is None and response is not None:
        try:
            code = int(response.status_code)
        except (TypeError, ValueError):
            code = None
    if code in (401, 403) or "unauthorized" in msg or "invalid api key" in msg:
        return FailureCategory.AUTHENTICATION, False
    if code == 404 or "unknown model" in msg or "does not exist" in msg:
        return FailureCategory.INVALID_MODEL, False
    if code == 429 or "rate limit" in msg:
        return FailureCategory.RATE_LIMITED, True
    if code is not None and code >= 500:
        return FailureCategory.SERVER_ERROR, True
    return FailureCategory.UNKNOWN, True


@dataclass
class RoutingRequest:
    task_type: str = "general"
    worker_role: str = ""
    requires_tool_calling: bool = False
    min_context: int = 0
    requires_vision: bool = False
    requires_reasoning: bool = False
    requires_streaming: bool = False
    requires_structured_output: bool = False
    min_coding: int = 0
    prefer_latency: str = ""
    prefer_cost: str = ""
    zero_cost_mode: bool = True
    preordered_providers: tuple[str, ...] = ()
    excluded_providers: tuple[str, ...] = ()


@dataclass
class RoutingDecision:
    provider: str
    model_id: str
    reason: str = ""
    candidates_considered: int = 0
    fallback_chain: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "reason": self.reason,
            "candidates_considered": self.candidates_considered,
            "fallback_chain": list(self.fallback_chain),
        }


_LATENCY_RANK = {"fast": 0, "standard": 1, "slow": 2}
_COST_RANK = {"free": 0, "cheap": 1, "standard": 2, "premium": 3}


def rank_candidates(
    request: RoutingRequest,
    catalog: Any,
    health: Any | None = None,
    configured_providers: list[str] | None = None,
) -> list[tuple[Any, str]]:
    """Rank (capability, reason) pairs following the 7-stage deterministic pipeline.

    Never falls back to a model that cannot satisfy required capabilities:
    tools / vision / structured output / streaming are hard filters.
    """
    # 1. Capability filter (never infer from OpenAI-compat alone).
    filter_kwargs: dict[str, Any] = {
        "min_context": request.min_context,
        "min_coding": request.min_coding,
    }
    if request.requires_tool_calling:
        filter_kwargs["tool_calling"] = True
        filter_kwargs["supports_tools"] = True
    if request.requires_vision:
        filter_kwargs["vision"] = True
        filter_kwargs["supports_vision"] = True
    if request.requires_structured_output:
        filter_kwargs["supports_structured_output"] = True
    if request.requires_streaming:
        filter_kwargs["supports_streaming"] = True
    try:
        cands = catalog.filter(**filter_kwargs)
    except TypeError:
        # Legacy catalogs without the extended flags.
        cands = catalog.filter(
            tool_calling=True if request.requires_tool_calling else None,
            min_context=request.min_context,
            vision=True if request.requires_vision else None,
            min_coding=request.min_coding,
        )
    if request.requires_reasoning:
        cands = [
            c
            for c in cands
            if getattr(c, "reasoning", False)
            or getattr(c, "supports_reasoning", False)
        ]

    # 2. Configured and excluded providers
    if configured_providers is not None:
        cands = [c for c in cands if c.provider in configured_providers]
    if request.excluded_providers:
        cands = [c for c in cands if c.provider not in request.excluded_providers]

    # 3. Zero-cost filter (BOB_ZERO_COST_MODE)
    if request.zero_cost_mode:
        from agent_system.services.providers import provider_spec

        filtered = []
        for c in cands:
            spec = provider_spec(c.provider)
            if c.cost_class == "free" or (spec is not None and spec.free_tier):
                filtered.append(c)
        cands = filtered

    # 4. Quota / health & cooldown filter
    health_tracker = health
    if health_tracker is None:
        from agent_system.services.provider_health import GLOBAL_HEALTH_TRACKER

        health_tracker = GLOBAL_HEALTH_TRACKER

    if health_tracker is not None:
        cands = [c for c in cands if health_tracker.is_routable(c.provider, c.model_id)]

    # Determine provider role order default if not specified
    role_order = list(request.preordered_providers)
    if not role_order:
        default_chain = [
            "groq",
            "gemini",
            "opencode",
            "nim",
            "openrouter",
            "ollama_cloud",
            "ollama",
        ]
        if request.task_type == "chat" or request.prefer_latency == "fast":
            role_order = default_chain
        elif request.min_coding >= 2 or "code" in request.worker_role.lower():
            role_order = [
                "opencode",
                "nim",
                "gemini",
                "groq",
                "openrouter",
                "ollama_cloud",
                "ollama",
            ]
        elif request.requires_reasoning:
            role_order = [
                "nim",
                "gemini",
                "opencode",
                "groq",
                "openrouter",
                "ollama_cloud",
                "ollama",
            ]
        elif request.requires_vision:
            role_order = ["gemini", "groq", "nim", "opencode", "openrouter"]
        else:
            role_order = default_chain

    def _score(cap: Any) -> tuple[int, int, int, int, str, str]:
        latency_pen = 0
        if request.prefer_latency == "fast":
            latency_pen = _LATENCY_RANK.get(cap.latency_class, 1)
        cost_pen = _COST_RANK.get(cap.cost_class, 1)
        if request.prefer_cost == "free":
            cost_pen = _COST_RANK.get(cap.cost_class, 1) * 2

        order_pen = 99
        if cap.provider in role_order:
            order_pen = role_order.index(cap.provider)

        coding_bonus = -int(cap.coding)
        return (order_pen, latency_pen, cost_pen, coding_bonus, cap.provider, cap.model_id)

    ranked = sorted(cands, key=_score)
    out: list[tuple[Any, str]] = []
    for cap in ranked:
        reason = (
            f"provider={cap.provider} model={cap.model_id} "
            f"tool_calling={cap.tool_calling} context={cap.context_limit} "
            f"coding={cap.coding} latency={cap.latency_class} cost={cap.cost_class}"
        )
        out.append((cap, reason))
    return out


def route(
    request: RoutingRequest,
    catalog: Any,
    health: Any | None = None,
    configured_providers: list[str] | None = None,
) -> RoutingDecision | None:
    """Select the best provider/model or return None when nothing qualifies."""
    ranked = rank_candidates(request, catalog, health, configured_providers)
    if not ranked:
        return None
    top, reason = ranked[0]
    chain = [f"{c.provider}/{c.model_id}" for c, _ in ranked[1:4]]
    return RoutingDecision(
        provider=top.provider,
        model_id=top.model_id,
        reason=reason,
        candidates_considered=len(ranked),
        fallback_chain=chain,
    )


def request_for_role(role_name: str, task_type: str = "general") -> RoutingRequest:
    """Build a RoutingRequest from the worker specialization registry."""
    from agent_system.services.worker_roles import role_for

    role = role_for(role_name)
    if role is None:
        return RoutingRequest(task_type=task_type, worker_role=role_name)
    return RoutingRequest(
        task_type=task_type,
        worker_role=role.role,
        requires_tool_calling=role.requires_tool_calling,
        min_context=role.min_context,
        requires_vision=role.requires_vision,
        min_coding=role.min_coding,
        prefer_latency=role.prefers_latency,
        preordered_providers=tuple(role.fallback_order),
    )


def request_for_mode(mode: str = "auto", task_type: str = "general") -> RoutingRequest:
    """Build a RoutingRequest for a specific routing mode.

    Modes:
    - auto: balanced general routing
    - auto/fast: prefers low latency
    - auto/coding: requires coding capabilities (min_coding >= 2)
    - auto/reasoning: requires reasoning capability
    - auto/cheap: prefers free / cheap models
    - auto/reliable: prefers premium/standard reliable providers
    - auto/offline: limits to offline/local providers (e.g. ollama)
    """
    m = (mode or "auto").strip().lower()
    if m in ("auto/fast", "fast"):
        return RoutingRequest(task_type=task_type, prefer_latency="fast")
    if m in ("auto/coding", "coding"):
        return RoutingRequest(task_type=task_type, min_coding=2, requires_tool_calling=True)
    if m in ("auto/reasoning", "reasoning"):
        return RoutingRequest(task_type=task_type, requires_reasoning=True)
    if m in ("auto/cheap", "cheap"):
        return RoutingRequest(task_type=task_type, prefer_cost="free")
    if m in ("auto/reliable", "reliable"):
        return RoutingRequest(task_type=task_type, prefer_cost="standard")
    if m in ("auto/offline", "offline"):
        return RoutingRequest(task_type=task_type, preordered_providers=("ollama",))
    return RoutingRequest(task_type=task_type)


__all__ = [
    "FailureCategory",
    "RoutingDecision",
    "RoutingRequest",
    "classify_error",
    "rank_candidates",
    "request_for_mode",
    "request_for_role",
    "route",
]
