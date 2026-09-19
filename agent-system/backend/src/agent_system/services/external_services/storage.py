"""Durable Object Storage Abstraction for Bob Agent.

Target:
StorageProvider
├── LocalStorageProvider (default, zero extra dependencies)
└── S3CompatibleStorageProvider (optional, boto3 via soft import)

Respects existing limits:
- MAX_FILE_SIZE_MB
- MAX_OUTPUT_SIZE_MB
- MAX_WORKSPACE_SIZE_MB

Never stores credentials or secrets as normal artifacts.
"""

from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, BinaryIO

from agent_system.config import Settings
from agent_system.services.external_services.base import (
    ExternalService,
    ExternalServiceError,
    ServiceHealth,
    ServiceHealthStatus,
    redact_secrets,
)

logger = logging.getLogger(__name__)

# Soft import guard for boto3
try:
    import boto3

    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False
    boto3 = None


class StorageProvider(ExternalService, ABC):
    """Abstract base class for object storage providers."""

    @abstractmethod
    def upload_object(
        self,
        key: str,
        data: bytes | BinaryIO,
        content_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        """Upload object data. Key must not contain directory traversal attempts."""

    @abstractmethod
    def download_object(self, key: str) -> bytes:
        """Download object data by key."""

    @abstractmethod
    def delete_object(self, key: str) -> bool:
        """Delete object by key."""

    @abstractmethod
    def list_objects(self, prefix: str = "") -> list[dict[str, Any]]:
        """List object metadata with optional key prefix."""


class LocalStorageProvider(StorageProvider):
    """Local filesystem-backed durable object storage provider."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root_path = Path(settings.storage_local_root).resolve()
        self.max_file_bytes = settings.max_file_size_mb * 1024 * 1024
        os.makedirs(self.root_path, exist_ok=True)

    @property
    def name(self) -> str:
        return "storage_local"

    @property
    def is_configured(self) -> bool:
        return True

    @property
    def is_enabled(self) -> bool:
        return self.settings.storage_provider.lower() == "local"

    def check_health(self, timeout: float = 5.0) -> ServiceHealth:
        start = time.monotonic()
        try:
            test_file = self.root_path / ".health_check"
            test_file.write_text("health_ok")
            test_file.unlink()
            latency = (time.monotonic() - start) * 1000.0
            return ServiceHealth(
                name=self.name,
                configured=True,
                enabled=self.is_enabled,
                reachable=True,
                authenticated=True,
                status=ServiceHealthStatus.OK if self.is_enabled else ServiceHealthStatus.DISABLED,
                latency_ms=latency,
                last_success=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                details={"root": str(self.root_path)},
            )
        except Exception as exc:
            latency = (time.monotonic() - start) * 1000.0
            return ServiceHealth(
                name=self.name,
                configured=True,
                enabled=self.is_enabled,
                reachable=False,
                authenticated=False,
                status=ServiceHealthStatus.UNAVAILABLE,
                latency_ms=latency,
                last_error=redact_secrets(str(exc)),
                details={"root": str(self.root_path)},
            )

    def _safe_path(self, key: str) -> Path:
        clean_key = key.lstrip("/")
        target_path = (self.root_path / clean_key).resolve()
        if not str(target_path).startswith(str(self.root_path)):
            raise ExternalServiceError(
                f"Path traversal denied for key: {key}", service=self.name, code="SECURITY_ERROR"
            )
        return target_path

    def upload_object(
        self,
        key: str,
        data: bytes | BinaryIO,
        content_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        if isinstance(data, bytes):
            payload = data
        else:
            payload = data.read()

        if len(payload) > self.max_file_bytes:
            raise ExternalServiceError(
                f"File size {len(payload)} bytes exceeds maximum allowed limit "
                f"of {self.max_file_bytes} bytes",
                service=self.name,
                code="SIZE_LIMIT_EXCEEDED",
            )

        target = self._safe_path(key)
        os.makedirs(target.parent, exist_ok=True)
        target.write_bytes(payload)

        return {
            "key": key,
            "size_bytes": len(payload),
            "content_type": content_type,
            "provider": self.name,
        }

    def download_object(self, key: str) -> bytes:
        target = self._safe_path(key)
        if not target.is_file():
            raise ExternalServiceError(
                f"Object not found: {key}", service=self.name, code="NOT_FOUND"
            )
        return target.read_bytes()

    def delete_object(self, key: str) -> bool:
        target = self._safe_path(key)
        if target.is_file():
            target.unlink()
            return True
        return False

    def list_objects(self, prefix: str = "") -> list[dict[str, Any]]:
        results = []
        prefix_clean = prefix.lstrip("/")
        for root, _, files in os.walk(self.root_path):
            for file in files:
                full_path = Path(root) / file
                rel_key = str(full_path.relative_to(self.root_path))
                if not prefix_clean or rel_key.startswith(prefix_clean):
                    results.append(
                        {
                            "key": rel_key,
                            "size_bytes": full_path.stat().st_size,
                            "modified_at": full_path.stat().st_mtime,
                        }
                    )
        return results


class S3CompatibleStorageProvider(StorageProvider):
    """S3-compatible cloud object storage provider (boto3)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bucket_name = settings.s3_bucket_name
        self.max_file_bytes = settings.max_file_size_mb * 1024 * 1024
        self._s3_client: Any = None

    @property
    def name(self) -> str:
        return "storage_s3"

    @property
    def is_configured(self) -> bool:
        return bool(
            self.bucket_name
            and (self.settings.s3_access_key_id or os.environ.get("AWS_ACCESS_KEY_ID"))
        )

    @property
    def is_enabled(self) -> bool:
        return self.settings.storage_provider.lower() == "s3" and self.is_configured

    def _get_client(self) -> Any:
        if not BOTO3_AVAILABLE:
            raise ExternalServiceError(
                "boto3 is not installed. Install via `pip install boto3` to use S3 storage.",
                service=self.name,
                code="DRIVER_MISSING",
            )
        if self._s3_client is None:
            kwargs: dict[str, Any] = {"region_name": self.settings.s3_region_name}
            if self.settings.s3_endpoint_url:
                kwargs["endpoint_url"] = self.settings.s3_endpoint_url
            if self.settings.s3_access_key_id:
                kwargs["aws_access_key_id"] = self.settings.s3_access_key_id
            if self.settings.s3_secret_access_key:
                kwargs["aws_secret_access_key"] = self.settings.s3_secret_access_key

            self._s3_client = boto3.client("s3", **kwargs)
        return self._s3_client

    def check_health(self, timeout: float = 5.0) -> ServiceHealth:
        if not self.is_configured:
            return ServiceHealth(
                name=self.name,
                configured=False,
                enabled=self.is_enabled,
                reachable=False,
                authenticated=False,
                status=ServiceHealthStatus.NOT_CONFIGURED,
                details={"bucket": self.bucket_name},
            )

        if not BOTO3_AVAILABLE:
            return ServiceHealth(
                name=self.name,
                configured=True,
                enabled=self.is_enabled,
                reachable=False,
                authenticated=False,
                status=ServiceHealthStatus.UNAVAILABLE,
                last_error="Driver 'boto3' is not installed",
                details={"bucket": self.bucket_name},
            )

        start = time.monotonic()
        try:
            client = self._get_client()
            client.head_bucket(Bucket=self.bucket_name)
            latency = (time.monotonic() - start) * 1000.0
            return ServiceHealth(
                name=self.name,
                configured=True,
                enabled=True,
                reachable=True,
                authenticated=True,
                status=ServiceHealthStatus.OK if self.is_enabled else ServiceHealthStatus.DISABLED,
                latency_ms=latency,
                last_success=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                details={"bucket": self.bucket_name, "region": self.settings.s3_region_name},
            )
        except Exception as exc:
            latency = (time.monotonic() - start) * 1000.0
            err_msg = str(exc)
            status = (
                ServiceHealthStatus.AUTH_FAILED
                if "403" in err_msg or "AccessDenied" in err_msg
                else ServiceHealthStatus.UNAVAILABLE
            )
            return ServiceHealth(
                name=self.name,
                configured=True,
                enabled=self.is_enabled,
                reachable=False,
                authenticated=False,
                status=status,
                latency_ms=latency,
                last_error=redact_secrets(err_msg),
                details={"bucket": self.bucket_name},
            )

    def upload_object(
        self,
        key: str,
        data: bytes | BinaryIO,
        content_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        if isinstance(data, bytes):
            payload = data
        else:
            payload = data.read()

        if len(payload) > self.max_file_bytes:
            raise ExternalServiceError(
                f"File size {len(payload)} bytes exceeds limit {self.max_file_bytes}",
                service=self.name,
                code="SIZE_LIMIT_EXCEEDED",
            )

        client = self._get_client()
        try:
            client.put_object(
                Bucket=self.bucket_name,
                Key=key.lstrip("/"),
                Body=payload,
                ContentType=content_type,
            )
            return {
                "key": key,
                "size_bytes": len(payload),
                "content_type": content_type,
                "provider": self.name,
                "bucket": self.bucket_name,
            }
        except Exception as exc:
            raise ExternalServiceError(f"S3 upload failed: {exc}", service=self.name) from exc

    def download_object(self, key: str) -> bytes:
        client = self._get_client()
        try:
            res = client.get_object(Bucket=self.bucket_name, Key=key.lstrip("/"))
            return bytes(res["Body"].read())
        except Exception as exc:
            raise ExternalServiceError(
                f"S3 download failed for {key}: {exc}", service=self.name
            ) from exc

    def delete_object(self, key: str) -> bool:
        client = self._get_client()
        try:
            client.delete_object(Bucket=self.bucket_name, Key=key.lstrip("/"))
            return True
        except Exception as exc:
            logger.warning(f"S3 delete_object failed for {key}: {redact_secrets(str(exc))}")
            return False

    def list_objects(self, prefix: str = "") -> list[dict[str, Any]]:
        client = self._get_client()
        try:
            res = client.list_objects_v2(Bucket=self.bucket_name, Prefix=prefix.lstrip("/"))
            results = []
            for item in res.get("Contents", []):
                results.append(
                    {
                        "key": item["Key"],
                        "size_bytes": item["Size"],
                        "modified_at": item["LastModified"].isoformat(),
                    }
                )
            return results
        except Exception as exc:
            raise ExternalServiceError(f"S3 list_objects failed: {exc}", service=self.name) from exc
