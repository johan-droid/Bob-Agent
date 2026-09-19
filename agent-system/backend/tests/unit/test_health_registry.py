"""Unit tests for Universal HealthRegistry."""

from agent_system.config import Settings
from agent_system.services.health import HealthRegistry


def test_health_registry_check_all():
    settings = Settings()
    registry = HealthRegistry(settings)
    res = registry.check_all(timeout=2.0)

    assert "status" in res
    assert "services" in res
    services = res["services"]

    # Verify key services are present
    assert "sqlite" in services or "postgresql" in services or "database" in services
    assert "redis" in services
    assert "mongodb" in services
    assert "storage_local" in services
    assert "llm_groq" in services
    assert "telegram" in services
    assert "mcp" in services
    assert "openconnector" in services
    assert "search_duckduckgo" in services
