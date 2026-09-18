"""Agentic Runtime v1 tests: capability catalog + health + router (additive)."""

from __future__ import annotations

from agent_system.services.llm_catalog import DEFAULT_CATALOG, capability_for
from agent_system.services.llm_router import (
    RoutingRequest,
    rank_candidates,
    request_for_role,
    route,
)
from agent_system.services.provider_health import (
    ProviderHealth,
    ProviderHealthTracker,
    classify_provider_error,
)
from agent_system.services.worker_roles import WORKER_ROLES, role_for


def test_catalog_has_all_provider_roles() -> None:
    providers = {m.provider for m in DEFAULT_CATALOG.all()}
    for expected in ("groq", "gemini", "nim", "ollama", "ollama_cloud", "openrouter"):
        assert expected in providers


def test_unknown_model_is_conservative_no_tool_calling() -> None:
    cap = capability_for("groq", "no-such-model-xyz")
    assert cap.tool_calling is False


def test_tool_calling_filter_never_offers_non_tool_models() -> None:
    req = RoutingRequest(requires_tool_calling=True, min_coding=2)
    ranked = rank_candidates(req, DEFAULT_CATALOG)
    assert ranked
    assert all(c.tool_calling for c, _ in ranked)
    # deepseek-r1 (nim) has no tool calling: must never appear here.
    assert all(c.model_id != "deepseek-ai/deepseek-r1" for c, _ in ranked)


def test_role_request_maps_coder_requirements() -> None:
    req = request_for_role("CODER")
    assert req.requires_tool_calling is True
    assert req.min_coding >= 2
    assert "nim" in req.preordered_providers


def test_router_avoids_rate_limited_provider() -> None:
    health = ProviderHealthTracker()
    health.report_failure(
        "gemini", "gemini-2.0-flash", "429 RATE_LIMITED", retry_after_seconds=600.0
    )
    req = RoutingRequest(requires_tool_calling=False)
    decision = route(req, DEFAULT_CATALOG, health)
    assert decision is not None
    assert decision.provider != "gemini" or health.is_routable(decision.provider, decision.model_id)


def test_health_classification() -> None:
    assert classify_provider_error("429 too many") == ProviderHealth.RATE_LIMITED
    assert classify_provider_error("401 bad key") == ProviderHealth.AUTH_FAILED
    assert classify_provider_error("500 internal") == ProviderHealth.UNAVAILABLE


def test_all_worker_roles_present() -> None:
    for role in (
        "RESEARCHER",
        "CODER",
        "DEBUGGER",
        "SECURITY_AUDITOR",
        "BROWSER_AGENT",
        "DATABASE_ENGINEER",
        "FRONTEND_ENGINEER",
        "BACKEND_ENGINEER",
        "TEST_ENGINEER",
        "DEVOPS_AGENT",
        "DOCUMENTATION_AGENT",
        "REVIEWER",
    ):
        assert role_for(role) is not None
    assert role_for("nope") is None
    assert WORKER_ROLES["CODER"].requires_tool_calling is True
