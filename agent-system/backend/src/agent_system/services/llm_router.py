"""Capability-aware LLM router (Agentic Runtime v1, additive)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RoutingRequest:
    task_type: str = "general"
    worker_role: str = ""
    requires_tool_calling: bool = False
    min_context: int = 0
    requires_vision: bool = False
    requires_reasoning: bool = False
    min_coding: int = 0
    prefer_latency: str = ""
    prefer_cost: str = ""
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
    """Rank (capability, reason) pairs. Pure function — no I/O, no mutation."""
    cands = catalog.filter(
        tool_calling=True if request.requires_tool_calling else None,
        min_context=request.min_context,
        vision=True if request.requires_vision else None,
        min_coding=request.min_coding,
    )
    if request.requires_reasoning:
        cands = [c for c in cands if c.reasoning]
    if configured_providers is not None:
        cands = [c for c in cands if c.provider in configured_providers]
    if request.excluded_providers:
        cands = [c for c in cands if c.provider not in request.excluded_providers]
    if health is not None:
        cands = [c for c in cands if health.is_routable(c.provider, c.model_id)]

    def _score(cap: Any) -> tuple[int, int, int, int, str, str]:
        latency_pen = 0
        if request.prefer_latency == "fast":
            latency_pen = _LATENCY_RANK.get(cap.latency_class, 1)
        cost_pen = _COST_RANK.get(cap.cost_class, 1)
        if request.prefer_cost == "free":
            cost_pen = _COST_RANK.get(cap.cost_class, 1) * 2
        order_pen = 0
        if request.preordered_providers and cap.provider in request.preordered_providers:
            order_pen = list(request.preordered_providers).index(cap.provider)
        elif request.preordered_providers:
            order_pen = len(request.preordered_providers)
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


__all__ = [
    "RoutingDecision",
    "RoutingRequest",
    "rank_candidates",
    "request_for_role",
    "route",
]
