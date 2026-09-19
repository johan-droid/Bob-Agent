"""Unit tests for Storage abstraction (LocalStorageProvider and S3CompatibleStorageProvider)."""

import tempfile
from unittest.mock import MagicMock, patch

import pytest

from agent_system.config import Settings
from agent_system.services.external_services.base import ExternalServiceError, ServiceHealthStatus
from agent_system.services.external_services.storage import (
    BOTO3_AVAILABLE,
    LocalStorageProvider,
    S3CompatibleStorageProvider,
)


def test_local_storage_provider_upload_download_delete():
    with tempfile.TemporaryDirectory() as tmpdir:
        settings = Settings(storage_provider="local", storage_local_root=tmpdir, max_file_size_mb=1)
        provider = LocalStorageProvider(settings)

        assert provider.is_enabled is True
        health = provider.check_health()
        assert health.status == ServiceHealthStatus.OK

        # Test upload
        res = provider.upload_object("test/data.txt", b"hello world", content_type="text/plain")
        assert res["key"] == "test/data.txt"
        assert res["size_bytes"] == 11

        # Test download
        downloaded = provider.download_object("test/data.txt")
        assert downloaded == b"hello world"

        # Test list
        items = provider.list_objects(prefix="test")
        assert len(items) == 1
        assert items[0]["key"] == "test/data.txt"

        # Test delete
        assert provider.delete_object("test/data.txt") is True
        assert provider.delete_object("test/data.txt") is False


def test_local_storage_size_limit_and_path_traversal():
    with tempfile.TemporaryDirectory() as tmpdir:
        settings = Settings(storage_provider="local", storage_local_root=tmpdir, max_file_size_mb=1)
        provider = LocalStorageProvider(settings)

        # Test size limit exceeded
        large_data = b"x" * (1024 * 1024 + 10)
        with pytest.raises(ExternalServiceError) as exc_info:
            provider.upload_object("oversized.bin", large_data)
        assert "exceeds maximum allowed limit" in str(exc_info.value)

        # Test path traversal prevention
        with pytest.raises(ExternalServiceError) as exc_info:
            provider.upload_object("../../../etc/passwd", b"bad_content")
        assert "Path traversal denied" in str(exc_info.value)


def test_s3_storage_provider_unconfigured():
    settings = Settings(storage_provider="s3", s3_bucket_name="")
    provider = S3CompatibleStorageProvider(settings)

    assert provider.is_configured is False
    health = provider.check_health()
    assert health.status == ServiceHealthStatus.NOT_CONFIGURED


@pytest.mark.skipif(not BOTO3_AVAILABLE, reason="boto3 not installed")
def test_s3_storage_provider_mock_operations():
    settings = Settings(
        storage_provider="s3",
        s3_bucket_name="my-bucket",
        s3_access_key_id="key123",
        s3_secret_access_key="secret456",
    )
    provider = S3CompatibleStorageProvider(settings)

    with patch("boto3.client") as mock_boto:
        mock_client = MagicMock()
        mock_boto.return_value = mock_client

        # Test health check
        health = provider.check_health()
        assert health.status == ServiceHealthStatus.OK
        assert "secret456" not in str(health.to_dict())

        # Test upload
        provider.upload_object("docs/test.pdf", b"pdf content")
        mock_client.put_object.assert_called_once_with(
            Bucket="my-bucket", Key="docs/test.pdf", Body=b"pdf content", ContentType="application/octet-stream"
        )
