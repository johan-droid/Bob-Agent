"""MongoDB Atlas Optional Adapter for Bob Agent.

MongoDB is an OPTIONAL secondary datastore.
It uses `pymongo` via a soft/optional import boundary if installed.
MongoDB is NOT a hard dependency for Bob startup.

Behavior:
- MongoDB disabled -> Bob works normally
- MongoDB unavailable -> unrelated Bob functions continue
- MongoDB invalid credentials -> clear health/config error
- Secret redaction for URIs in errors & logs
"""

from __future__ import annotations

import logging
import time
from typing import Any

from agent_system.config import Settings
from agent_system.services.external_services.base import (
    ExternalService,
    ExternalServiceError,
    ServiceDisabledError,
    ServiceHealth,
    ServiceHealthStatus,
    redact_secrets,
    with_retry,
)

logger = logging.getLogger(__name__)

# Optional import guard for pymongo
try:
    import pymongo
    from pymongo.errors import (
        ConnectionFailure,
        OperationFailure,
        ServerSelectionTimeoutError,
    )

    PYMONGO_AVAILABLE = True
except ImportError:
    PYMONGO_AVAILABLE = False
    pymongo = None


class MongoDBAdapter(ExternalService):
    """Adapter and repository boundary for optional MongoDB secondary storage."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: Any = None

    @property
    def name(self) -> str:
        return "mongodb"

    @property
    def is_configured(self) -> bool:
        return bool(str(self.settings.mongodb_uri).strip())

    @property
    def is_enabled(self) -> bool:
        return self.settings.mongodb_enabled and self.is_configured

    def _get_client(self, timeout_ms: int | None = None) -> Any:
        if not PYMONGO_AVAILABLE:
            raise ExternalServiceError(
                "pymongo is not installed. Install via `pip install pymongo` to use MongoDB.",
                service=self.name,
                code="DRIVER_MISSING",
            )
        if not self.is_enabled:
            raise ServiceDisabledError("MongoDB is disabled in settings", service=self.name)

        if self._client is None:
            timeout = timeout_ms or self.settings.mongodb_connect_timeout_ms
            self._client = pymongo.MongoClient(
                self.settings.mongodb_uri,
                serverSelectionTimeoutMS=timeout,
                connectTimeoutMS=timeout,
            )
        return self._client

    def check_health(self, timeout: float = 5.0) -> ServiceHealth:
        if not self.settings.mongodb_enabled:
            return ServiceHealth(
                name=self.name,
                configured=self.is_configured,
                enabled=False,
                reachable=False,
                authenticated=False,
                status=ServiceHealthStatus.DISABLED,
                details={"reason": "mongodb_enabled is False"},
            )

        if not self.is_configured:
            return ServiceHealth(
                name=self.name,
                configured=False,
                enabled=True,
                reachable=False,
                authenticated=False,
                status=ServiceHealthStatus.NOT_CONFIGURED,
                details={"reason": "mongodb_uri is empty"},
            )

        if not PYMONGO_AVAILABLE:
            return ServiceHealth(
                name=self.name,
                configured=True,
                enabled=True,
                reachable=False,
                authenticated=False,
                status=ServiceHealthStatus.UNAVAILABLE,
                last_error="Driver 'pymongo' is not installed",
                details={"reason": "pymongo_missing"},
            )

        start = time.monotonic()
        timeout_ms = int(timeout * 1000)
        clean_uri = redact_secrets(self.settings.mongodb_uri)

        try:
            client = pymongo.MongoClient(
                self.settings.mongodb_uri,
                serverSelectionTimeoutMS=timeout_ms,
                connectTimeoutMS=timeout_ms,
            )
            client.admin.command("ping")
            latency = (time.monotonic() - start) * 1000.0
            client.close()
            return ServiceHealth(
                name=self.name,
                configured=True,
                enabled=True,
                reachable=True,
                authenticated=True,
                status=ServiceHealthStatus.OK,
                latency_ms=latency,
                last_success=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                details={"uri": clean_uri, "database": self.settings.mongodb_database},
            )
        except OperationFailure as exc:
            latency = (time.monotonic() - start) * 1000.0
            return ServiceHealth(
                name=self.name,
                configured=True,
                enabled=True,
                reachable=True,
                authenticated=False,
                status=ServiceHealthStatus.AUTH_FAILED,
                latency_ms=latency,
                last_error=redact_secrets(str(exc)),
                details={"uri": clean_uri},
            )
        except (ServerSelectionTimeoutError, ConnectionFailure) as exc:
            latency = (time.monotonic() - start) * 1000.0
            return ServiceHealth(
                name=self.name,
                configured=True,
                enabled=True,
                reachable=False,
                authenticated=False,
                status=ServiceHealthStatus.UNAVAILABLE,
                latency_ms=latency,
                last_error=redact_secrets(str(exc)),
                details={"uri": clean_uri},
            )
        except Exception as exc:
            latency = (time.monotonic() - start) * 1000.0
            return ServiceHealth(
                name=self.name,
                configured=True,
                enabled=True,
                reachable=False,
                authenticated=False,
                status=ServiceHealthStatus.UNAVAILABLE,
                latency_ms=latency,
                last_error=redact_secrets(str(exc)),
                details={"uri": clean_uri},
            )

    def get_collection(self, collection_name: str) -> Any:
        client = self._get_client()
        db = client[self.settings.mongodb_database]
        return db[collection_name]

    def insert_document(self, collection_name: str, document: dict[str, Any]) -> str:
        if not self.is_enabled:
            raise ServiceDisabledError("MongoDB is disabled", service=self.name)

        def _op() -> str:
            coll = self.get_collection(collection_name)
            res = coll.insert_one(document)
            return str(res.inserted_id)

        try:
            return with_retry(_op, max_retries=2)
        except Exception as exc:
            raise ExternalServiceError(
                f"MongoDB insert_document failed: {exc}", service=self.name
            ) from exc

    def find_documents(
        self, collection_name: str, query: dict[str, Any], limit: int = 100
    ) -> list[dict[str, Any]]:
        if not self.is_enabled:
            raise ServiceDisabledError("MongoDB is disabled", service=self.name)

        def _op() -> list[dict[str, Any]]:
            coll = self.get_collection(collection_name)
            return list(coll.find(query).limit(limit))

        try:
            return with_retry(_op, max_retries=2)
        except Exception as exc:
            raise ExternalServiceError(
                f"MongoDB find_documents failed: {exc}", service=self.name
            ) from exc

    def update_documents(
        self, collection_name: str, query: dict[str, Any], update: dict[str, Any]
    ) -> int:
        if not self.is_enabled:
            raise ServiceDisabledError("MongoDB is disabled", service=self.name)

        def _op() -> int:
            coll = self.get_collection(collection_name)
            res = coll.update_many(query, {"$set": update})
            return int(res.modified_count)

        try:
            return with_retry(_op, max_retries=2)
        except Exception as exc:
            raise ExternalServiceError(
                f"MongoDB update_documents failed: {exc}", service=self.name
            ) from exc

    def delete_documents(self, collection_name: str, query: dict[str, Any]) -> int:
        if not self.is_enabled:
            raise ServiceDisabledError("MongoDB is disabled", service=self.name)

        def _op() -> int:
            coll = self.get_collection(collection_name)
            res = coll.delete_many(query)
            return int(res.deleted_count)

        try:
            return with_retry(_op, max_retries=2)
        except Exception as exc:
            raise ExternalServiceError(
                f"MongoDB delete_documents failed: {exc}", service=self.name
            ) from exc

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
