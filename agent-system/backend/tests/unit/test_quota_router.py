"""Unit tests for quota-aware model router, zero-cost filtering, and rate limit tracking."""

from __future__ import annotations

from agent_system.services.llm_catalog import default_catalog
from agent_system.services.llm_router import RoutingRequest, rank_candidates, route
from agent_system.services.provider_health import ProviderHealth, ProviderHealthTracker


def test_zero_cost_mode_filters_paid_models() -> None:
    catalog = default_catalog()
    # Request with zero_cost_mode = True
    req = RoutingRequest(task_type="chat", zero_cost_mode=True)
    ranked = rank_candidates(req, catalog)
    assert len(ranked) > 0
    # Every ranked candidate must be cost_class == "free"
    for cap, _ in ranked:
        assert cap.cost_class == "free"


def test_429_cooldown_and_rate_limit_header_parsing() -> None:
    tracker = ProviderHealthTracker()
    # Report 429 rate limit
    tracker.report_failure(
        "groq",
        "llama-3.3-70b-versatile",
        "429 Rate Limit Exceeded",
        retry_after_seconds=30.0,
    )

    status = tracker.get("groq", "llama-3.3-70b-versatile")
    assert status.health == ProviderHealth.RATE_LIMITED
    assert tracker.is_routable("groq", "llama-3.3-70b-versatile") is False

    # Header parsing
    headers = {
        "x-ratelimit-remaining-requests": "0",
        "x-ratelimit-remaining-tokens": "100",
        "retry-after": "45",
    }
    tracker.update_rate_limits("groq", "llama-3.1-8b-instant", headers)
    st_instant = tracker.get("groq", "llama-3.1-8b-instant")
    assert st_instant.requests_remaining == 0
    assert st_instant.health == ProviderHealth.RATE_LIMITED
    assert tracker.is_routable("groq", "llama-3.1-8b-instant") is False


def test_router_role_ranking() -> None:
    catalog = default_catalog()

    # Fast / Chat role prefers Groq
    req_chat = RoutingRequest(task_type="chat", prefer_latency="fast")
    decision_chat = route(req_chat, catalog)
    assert decision_chat is not None
    assert decision_chat.provider == "groq"

    # Coding task prefers OpenCode
    req_code = RoutingRequest(task_type="coding", worker_role="CODER", min_coding=2)
    decision_code = route(req_code, catalog)
    assert decision_code is not None
    assert decision_code.provider in ("opencode", "nim")
