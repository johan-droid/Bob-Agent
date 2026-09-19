"""Unit tests for LLMProviderService adapter."""

from agent_system.config import Settings
from agent_system.services.external_services.base import ServiceHealthStatus
from agent_system.services.external_services.llm_service import LLMProviderService
from agent_system.services.provider_health import ProviderHealthTracker


def test_llm_service_unconfigured():
    settings = Settings(groq_api_key="")
    service = LLMProviderService("groq", settings)
    assert service.is_configured is False
    health = service.check_health()
    assert health.status == ServiceHealthStatus.NOT_CONFIGURED


def test_llm_service_configured_and_health_tracker():
    settings = Settings(groq_api_key="gsk_secret123")
    tracker = ProviderHealthTracker()

    service = LLMProviderService("groq", settings, tracker=tracker)
    assert service.is_configured is True
    health = service.check_health()
    assert health.status == ServiceHealthStatus.OK

    # Simulate health tracker failure
    tracker.report_failure("groq", error="429 Rate Limit Exceeded")
    health = service.check_health()
    assert health.status == ServiceHealthStatus.RATE_LIMITED
    assert "gsk_secret123" not in str(health.to_dict())
