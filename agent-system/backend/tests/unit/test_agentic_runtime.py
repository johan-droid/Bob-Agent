"""Agentic Runtime v1 tests: capability catalog + provider health (additive)."""

from __future__ import annotations

from agent_system.services.llm_catalog import DEFAULT_CATALOG, capability_for
from agent_system.services.provider_health import ProviderHealth, classify_provider_error


def test_catalog_has_all_provider_roles() -> None:
    providers = {m.provider for m in DEFAULT_CATALOG.all()}
    for expected in ("groq", "gemini", "nim", "ollama", "ollama_cloud", "openrouter"):
        assert expected in providers


def test_unknown_model_is_conservative_no_tool_calling() -> None:
    cap = capability_for("groq", "no-such-model-xyz")
    assert cap.tool_calling is False


def test_health_classification() -> None:
    assert classify_provider_error("429 too many") == ProviderHealth.RATE_LIMITED
    assert classify_provider_error("401 bad key") == ProviderHealth.AUTH_FAILED
    assert classify_provider_error("500 internal") == ProviderHealth.UNAVAILABLE
