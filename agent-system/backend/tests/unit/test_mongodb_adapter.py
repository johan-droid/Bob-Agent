"""Unit tests for MongoDBAdapter."""

from unittest.mock import MagicMock, patch

import pytest

from agent_system.config import Settings
from agent_system.services.external_services.base import ServiceDisabledError, ServiceHealthStatus
from agent_system.services.external_services.mongodb import PYMONGO_AVAILABLE, MongoDBAdapter


def test_mongodb_disabled_by_default():
    settings = Settings(mongodb_enabled=False, mongodb_uri="mongodb://localhost:27017")
    adapter = MongoDBAdapter(settings)

    assert adapter.is_enabled is False
    health = adapter.check_health()
    assert health.status == ServiceHealthStatus.DISABLED
    assert health.configured is True
    assert health.enabled is False

    with pytest.raises(ServiceDisabledError):
        adapter.insert_document("test_col", {"key": "val"})


def test_mongodb_not_configured():
    settings = Settings(mongodb_enabled=True, mongodb_uri="")
    adapter = MongoDBAdapter(settings)

    assert adapter.is_configured is False
    assert adapter.is_enabled is False
    health = adapter.check_health()
    assert health.status == ServiceHealthStatus.NOT_CONFIGURED


@patch("agent_system.services.external_services.mongodb.PYMONGO_AVAILABLE", False)
def test_mongodb_pymongo_missing():
    settings = Settings(mongodb_enabled=True, mongodb_uri="mongodb://localhost:27017")
    adapter = MongoDBAdapter(settings)

    health = adapter.check_health()
    assert health.status == ServiceHealthStatus.UNAVAILABLE
    assert "pymongo" in health.last_error


@pytest.mark.skipif(not PYMONGO_AVAILABLE, reason="pymongo not installed")
def test_mongodb_health_check_mock_success():
    settings = Settings(mongodb_enabled=True, mongodb_uri="mongodb://user:pass123@localhost:27017")
    adapter = MongoDBAdapter(settings)

    with patch("pymongo.MongoClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.admin.command.return_value = {"ok": 1}

        health = adapter.check_health()
        assert health.status == ServiceHealthStatus.OK
        assert health.reachable is True
        assert health.authenticated is True
        assert "pass123" not in health.details["uri"]
        assert "pass123" not in str(health.to_dict())


@pytest.mark.skipif(not PYMONGO_AVAILABLE, reason="pymongo not installed")
def test_mongodb_health_check_auth_failure():
    from pymongo.errors import OperationFailure

    settings = Settings(
        mongodb_enabled=True, mongodb_uri="mongodb://user:wrongpass@localhost:27017"
    )
    adapter = MongoDBAdapter(settings)

    with patch("pymongo.MongoClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.admin.command.side_effect = OperationFailure(
            "Authentication failed for user:wrongpass"
        )

        health = adapter.check_health()
        assert health.status == ServiceHealthStatus.AUTH_FAILED
        assert health.authenticated is False
        assert "wrongpass" not in str(health.to_dict())


@pytest.mark.skipif(not PYMONGO_AVAILABLE, reason="pymongo not installed")
def test_mongodb_crud_operations():
    settings = Settings(
        mongodb_enabled=True, mongodb_uri="mongodb://localhost:27017", mongodb_database="testdb"
    )
    adapter = MongoDBAdapter(settings)

    with patch("pymongo.MongoClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_db = MagicMock()
        mock_client.__getitem__.return_value = mock_db
        mock_coll = MagicMock()
        mock_db.__getitem__.return_value = mock_coll

        # Test insert
        mock_coll.insert_one.return_value.inserted_id = "obj_123"
        doc_id = adapter.insert_document("items", {"name": "test_item"})
        assert doc_id == "obj_123"
        mock_coll.insert_one.assert_called_once_with({"name": "test_item"})

        # Test find
        mock_coll.find.return_value.limit.return_value = [{"_id": "obj_123", "name": "test_item"}]
        docs = adapter.find_documents("items", {"name": "test_item"})
        assert len(docs) == 1
        assert docs[0]["name"] == "test_item"

        # Test update
        mock_coll.update_many.return_value.modified_count = 2
        updated = adapter.update_documents("items", {"name": "test_item"}, {"status": "active"})
        assert updated == 2

        # Test delete
        mock_coll.delete_many.return_value.deleted_count = 1
        deleted = adapter.delete_documents("items", {"name": "test_item"})
        assert deleted == 1
