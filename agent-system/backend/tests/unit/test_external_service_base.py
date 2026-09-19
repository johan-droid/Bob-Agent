"""Unit tests for ExternalService base abstraction and utilities."""

from agent_system.services.external_services.base import (
    ExternalServiceError,
    ServiceHealth,
    ServiceHealthStatus,
    ServiceUnavailableError,
    redact_secrets,
    with_retry,
)


def test_redact_secrets():
    raw_mongo = "mongodb+srv://admin:SecretPass123@cluster.mongodb.net/dbname"
    redacted_mongo = redact_secrets(raw_mongo)
    assert "SecretPass123" not in redacted_mongo
    assert "admin:***@" in redacted_mongo

    raw_pg = "postgresql://user:p%40ssword@localhost:5432/neondb"
    redacted_pg = redact_secrets(raw_pg)
    assert "p%40ssword" not in redacted_pg
    assert "user:***@" in redacted_pg

    raw_token = "api_key=sk-proj-1234567890abcdef"
    redacted_token = redact_secrets(raw_token)
    assert "1234567890abcdef" not in redacted_token
    assert "api_key=***" in redacted_token


def test_service_health_to_dict_redacts_secrets():
    health = ServiceHealth(
        name="test_service",
        configured=True,
        enabled=True,
        reachable=False,
        authenticated=False,
        status=ServiceHealthStatus.UNAVAILABLE,
        last_error="Connection failed to postgresql://bob:secret123@db.example.com/mydb",
        details={"connection_string": "mongodb://admin:pass456@mongo.example.com"},
    )
    d = health.to_dict()
    assert "secret123" not in d["last_error"]
    assert "pass456" not in d["details"]["connection_string"]


def test_external_service_error_redacts_message():
    err = ExternalServiceError("Failed with password super_secret_val", service="test")
    assert "super_secret_val" not in str(err)
    assert "password ***" in str(err)


def test_with_retry_success():
    calls = 0

    def flaky():
        nonlocal calls
        calls += 1
        if calls < 2:
            raise ServiceUnavailableError("transient network issue")
        return "success"

    res = with_retry(flaky, max_retries=3, backoff_factor=0.01)
    assert res == "success"
    assert calls == 2
