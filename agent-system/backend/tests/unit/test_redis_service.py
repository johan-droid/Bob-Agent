"""Unit tests for RedisService adapter."""

from unittest.mock import MagicMock

from agent_system.config import Settings
from agent_system.services.external_services.base import ServiceHealthStatus
from agent_system.services.external_services.redis_service import RedisService


def test_redis_service_unconfigured():
    settings = Settings(redis_url="")
    service = RedisService(settings)
    assert service.is_configured is False
    health = service.check_health()
    assert health.status == ServiceHealthStatus.NOT_CONFIGURED


def test_redis_service_health_check_success():
    settings = Settings(redis_url="redis://:pass123@localhost:6379/0")
    mock_redis = MagicMock()
    mock_redis.ping.return_value = True
    mock_redis.info.return_value = {"used_memory_human": "2.5M"}

    service = RedisService(settings, redis_client=mock_redis)
    health = service.check_health()

    assert health.status == ServiceHealthStatus.OK
    assert health.reachable is True
    assert health.authenticated is True
    assert "pass123" not in health.details["url"]
    assert "pass123" not in str(health.to_dict())


def test_redis_service_locks_and_cache():
    settings = Settings(redis_url="redis://localhost:6379/0")
    mock_redis = MagicMock()
    service = RedisService(settings, redis_client=mock_redis)

    # Test lock acquisition
    mock_redis.set.return_value = True
    assert service.acquire_lock("job_1", ttl_seconds=30) is True
    mock_redis.set.assert_called_with("lock:job_1", "locked", ex=30, nx=True)

    # Test lock release
    mock_redis.delete.return_value = 1
    assert service.release_lock("job_1") is True

    # Test task deduplication
    mock_redis.set.return_value = False  # Set returning False means key already existed (duplicate)
    assert service.is_duplicate_task("task_999") is True

    # Test get/set state
    mock_redis.get.return_value = "cached_val"
    assert service.get_state("my_key") == "cached_val"

    mock_redis.set.return_value = True
    assert service.set_state("my_key", "new_val", ttl_seconds=60) is True


def test_redis_unreachable_fallback_isolation():
    from redis.exceptions import ConnectionError as RedisConnectionError

    settings = Settings(redis_url="redis://invalid_host:6379/0")
    mock_redis = MagicMock()
    mock_redis.ping.side_effect = RedisConnectionError(
        "Could not connect to Redis at invalid_host:6379"
    )

    service = RedisService(settings, redis_client=mock_redis)
    health = service.check_health()

    assert health.status == ServiceHealthStatus.UNAVAILABLE
    assert health.reachable is False
    assert "Could not connect" in health.last_error

    # Locks should safely fail without raising uncaught exceptions
    mock_redis.set.side_effect = RedisConnectionError("Down")
    assert service.acquire_lock("job_2") is False
    assert service.is_duplicate_task("task_1") is False
