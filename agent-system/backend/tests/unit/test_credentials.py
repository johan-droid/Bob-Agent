"""Unit tests for CredentialStore and envelope encryption vault."""

import pytest

from agent_system.infra.db import make_engine, make_session_factory
from agent_system.infra.models import Base, User, UserCredential
from agent_system.services.context import sanitize_context
from agent_system.services.credentials import CredentialStore


@pytest.fixture
def db_factory(tmp_path):
    db_file = tmp_path / "test_credentials.db"
    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        session.add(User(id="usr_01", display_name="User 1", auth_provider="telegram", role="admin"))
        session.add(User(id="usr_02", display_name="User 2", auth_provider="telegram", role="member"))
        session.commit()
    return factory


def test_credential_save_and_get(db_factory):
    vault = CredentialStore(db_factory)
    meta = vault.save(
        user_id="usr_01",
        provider="github",
        name="personal",
        payload={"token": "ghp_secret_token_12345"},
    )
    assert meta.provider == "github"
    assert meta.name == "personal"
    assert meta.status == "healthy"

    payload = vault.get("usr_01", "github", "personal")
    assert payload == {"token": "ghp_secret_token_12345"}


def test_user_isolation(db_factory):
    vault = CredentialStore(db_factory)
    vault.save(
        user_id="usr_01",
        provider="ssh",
        name="home-server",
        payload={"host": "10.0.0.1", "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\n..."},
    )

    payload_u2 = vault.get("usr_02", "ssh", "home-server")
    assert payload_u2 is None

    meta_u2 = vault.list_metadata("usr_02")
    assert len(meta_u2) == 0

    meta_u1 = vault.list_metadata("usr_01")
    assert len(meta_u1) == 1
    assert meta_u1[0].name == "home-server"


def test_credential_encryption_at_rest(db_factory):
    vault = CredentialStore(db_factory)
    secret_token = "ghp_super_secret_key_abcdef"
    vault.save("usr_01", "github", "work", {"token": secret_token})

    with db_factory() as session:
        row = (
            session.query(UserCredential)
            .filter_by(user_id="usr_01", provider="github", name="work")
            .one()
        )
        assert secret_token not in row.encrypted_blob
        assert secret_token not in row.encrypted_dek


def test_credential_revocation_and_deletion(db_factory):
    vault = CredentialStore(db_factory)
    vault.save("usr_01", "groq", "personal", {"api_key": "gsk_12345"})

    assert vault.get("usr_01", "groq", "personal") == {"api_key": "gsk_12345"}

    assert vault.revoke("usr_01", "groq", "personal") is True
    assert vault.get("usr_01", "groq", "personal") is None

    meta = vault.list_metadata("usr_01", "groq")
    assert len(meta) == 1
    assert meta[0].status == "revoked"

    assert vault.delete("usr_01", "groq", "personal") is True
    assert len(vault.list_metadata("usr_01")) == 0


def test_context_sanitization():
    raw_context = {
        "task_id": "task_123",
        "credential_ref": "ssh:home-server",
        "api_key": "gsk_secret_key_12345",
        "nested": {
            "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n-----END OPENSSH PRIVATE KEY-----"
        },
    }
    cleaned = sanitize_context(raw_context)
    assert cleaned["credential_ref"] == "ssh:home-server"
    assert cleaned["api_key"] == "[REDACTED]"
    assert cleaned["nested"]["private_key"] == "[REDACTED]"
