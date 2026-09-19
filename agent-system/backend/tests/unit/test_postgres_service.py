"""Unit tests for PostgresService adapter & database hardening."""

from agent_system.services.external_services.base import ServiceHealthStatus
from agent_system.services.external_services.postgres import PostgresService


def test_postgres_service_sqlite_health():
    service = PostgresService(database_url="sqlite:///:memory:")
    health = service.check_health()
    assert health.configured is True
    assert health.enabled is True
    assert health.reachable is True
    assert health.authenticated is True
    assert health.status == ServiceHealthStatus.OK


def test_postgres_service_unreachable():
    # Test invalid connection URL / unreachable host
    service = PostgresService(database_url="postgresql://user:pass@127.0.0.1:54329/invalid_db")
    health = service.check_health(timeout=1.0)
    assert health.configured is True
    assert health.reachable is False
    assert health.status in (ServiceHealthStatus.UNAVAILABLE, ServiceHealthStatus.AUTH_FAILED)
    assert "pass" not in str(health.to_dict())


def test_postgres_service_unconfigured():
    service = PostgresService(database_url="")
    health = service.check_health()
    assert health.configured is False
    assert health.status == ServiceHealthStatus.NOT_CONFIGURED
