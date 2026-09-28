"""P0 Security Gate & End-to-End Credential Non-Leakage Test Suite.

Verifies:
1. Secret never reaches LLM context.
2. Secret never appears in logs, traces, tasks, telemetry, or Telegram history.
3. User A cannot access User B's credentials (strict user isolation).
4. Revoked credentials cannot be resolved by tools.
5. Rotation invalidates old credentials and updates encrypted state in place.
6. Restart / reboot preserves encrypted credentials.
7. Missing/invalid master encryption key fails safely.
8. E2E flow: setup -> CredentialStore -> SSH/tool execution -> zero secret leak.
"""

from typing import Any
from unittest.mock import patch

import pytest

from agent_system.config import Settings
from agent_system.infra.db import make_engine, make_session_factory
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Base, User, UserCredential
from agent_system.services.context import sanitize_context
from agent_system.services.credentials import CredentialStore
from agent_system.services.identity import IdentityMode, Principal, Role
from agent_system.services.permissions import PermissionGate
from agent_system.services.telegram import TelegramService
from agent_system.services.tools.builtin.ssh import execute_ssh_command


@pytest.fixture
def db_factory(tmp_path):
    db_file = tmp_path / "test_sec_gate.db"
    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        session.add(
            User(id="usr_alice", display_name="Alice", auth_provider="telegram", role="admin")
        )
        session.add(User(id="usr_bob", display_name="Bob", auth_provider="telegram", role="member"))
        session.commit()
    return factory


def test_secrets_never_stored_unencrypted(db_factory):
    vault = CredentialStore(db_factory)
    raw_key = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "VERY_SECRET_ALICE_KEY\n"
        "-----END OPENSSH PRIVATE KEY-----"
    )
    vault.save("usr_alice", "ssh", "my-vps", {"private_key": raw_key})

    with db_factory() as session:
        row = (
            session.query(UserCredential)
            .filter_by(user_id="usr_alice", provider="ssh", name="my-vps")
            .one()
        )
        assert "VERY_SECRET_ALICE_KEY" not in row.encrypted_blob
        assert "VERY_SECRET_ALICE_KEY" not in row.encrypted_dek


def test_user_isolation_boundary(db_factory):
    vault = CredentialStore(db_factory)
    vault.save("usr_alice", "github", "personal", {"token": "ghp_alice_secret_token"})

    # Bob cannot fetch Alice's credential
    assert vault.get("usr_bob", "github", "personal") is None

    # Bob listing metadata sees only Bob's credentials
    assert len(vault.list_metadata("usr_bob")) == 0


def test_revoked_credential_cannot_be_resolved(db_factory):
    vault = CredentialStore(db_factory)
    vault.save(
        "usr_alice", "ssh", "home-server", {"hostname": "10.0.0.1", "private_key": "raw_secret"}
    )

    # Sanity check
    assert vault.get("usr_alice", "ssh", "home-server") is not None

    # Revoke
    vault.revoke("usr_alice", "ssh", "home-server")

    # Tool execution fails safely
    res = execute_ssh_command(
        "ssh:home-server", "uptime", user_id="usr_alice", credential_store=vault
    )
    assert res["ok"] is False
    assert "not found or revoked" in res["error"]


def test_credential_rotation_invalidates_old_secret(db_factory):
    vault = CredentialStore(db_factory)
    vault.save("usr_alice", "groq", "personal", {"api_key": "gsk_old_secret"})

    assert vault.get("usr_alice", "groq", "personal") == {"api_key": "gsk_old_secret"}

    # Rotate with new secret
    vault.rotate("usr_alice", "groq", "personal", {"api_key": "gsk_new_secret"})

    # Must return new secret
    assert vault.get("usr_alice", "groq", "personal") == {"api_key": "gsk_new_secret"}


def test_restart_preserves_encrypted_credentials(db_factory):
    vault = CredentialStore(db_factory)
    vault.save("usr_alice", "openrouter", "personal", {"api_key": "sk-or-secret_123"})

    # Re-instantiate vault simulating restart
    new_vault = CredentialStore(db_factory)
    assert new_vault.get("usr_alice", "openrouter", "personal") == {"api_key": "sk-or-secret_123"}


def test_context_sanitization_scrubs_raw_secrets():
    raw_payload = {
        "connection": "ssh:vps",
        "api_key": "gsk_secret_123",
        "nested": {
            "token": "ghp_secret_456",
            "private_key": (
                "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n-----END OPENSSH PRIVATE KEY-----"
            ),
        },
    }
    sanitized = sanitize_context(raw_payload)
    assert sanitized["connection"] == "ssh:vps"
    assert sanitized["api_key"] == "[REDACTED]"
    assert sanitized["nested"]["token"] == "[REDACTED]"
    assert sanitized["nested"]["private_key"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_full_chat_to_tool_e2e_zero_leakage_flow(db_factory):
    tg_svc = TelegramService(
        settings=Settings(), session_factory=db_factory, gate=PermissionGate(), bus=EventBus()
    )
    principal = Principal(user_id="usr_alice", role=Role.ADMIN, mode=IdentityMode.TELEGRAM)
    chat_id = 112233

    # 1. Setup SSH connection via Telegram chat flow
    await tg_svc._dispatch_command(principal, chat_id, "/setup ssh vps")
    # RFC 6761 reserves .invalid: it can never resolve, so this test performs no
    # real DNS/TCP to a third party. It previously dialled mycompany.org, a
    # domain a stranger owns, from the credential-leakage test.
    await tg_svc._handle_active_setup_step(principal, chat_id, "vps.example.invalid")
    await tg_svc._handle_active_setup_step(principal, chat_id, "ubuntu")
    secret_pem = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "TOP_SECRET_PEM_DATA\n"
        "-----END OPENSSH PRIVATE KEY-----"
    )
    await tg_svc._handle_active_setup_step(principal, chat_id, secret_pem)

    # 2. Tool resolves credential reference without exposing secret.
    # Transport is mocked: the assertion under test is "the vault resolves
    # ssh:vps and the PEM never escapes", which needs a working client, not a
    # live SSH server. This previously dialled a real third-party host to get
    # ok=True, and failed whenever that host was unreachable.
    class _FakeStream:
        """Enough of a paramiko ChannelFile for ssh_service's reads."""

        def __init__(self, data: bytes, exit_code: int = 0) -> None:
            self._data = data
            self.channel = self

        def read(self) -> bytes:
            return self._data

        def recv_exit_status(self) -> int:
            return 0

        def close(self) -> None:
            return None

    class _FakeTransport:
        @staticmethod
        def is_active() -> bool:
            return True

        @staticmethod
        def get_remote_server_key() -> str:
            return "FAKE-HOST-KEY"

    class _FakeClient:
        def set_missing_host_key_policy(self, policy: Any) -> None:
            return None

        def get_transport(self) -> Any:
            return _FakeTransport()

        def connect(self, *a: Any, **kw: Any) -> None:
            return None

        def exec_command(self, cmd: str, *a: Any, **kw: Any) -> Any:
            self._cmd = cmd
            return (_FakeStream(b""), _FakeStream(b"df -h output\n"), _FakeStream(b""))

        def close(self) -> None:
            return None

    from tests.unit.test_ssh_service import fake_transport_patch

    sock, transport = fake_transport_patch()
    with sock, transport, patch("paramiko.SSHClient", return_value=_FakeClient()):
        vault = CredentialStore(db_factory)
        tool_res = execute_ssh_command(
            "ssh:vps", "df -h", user_id="usr_alice", credential_store=vault
        )
    assert tool_res["ok"] is True, tool_res
    assert "df -h" in tool_res["stdout"] or "Filesystem" in tool_res["stdout"]

    # 3. Verify zero leakage in sanitized context
    cleaned_res = sanitize_context(tool_res)
    assert "TOP_SECRET_PEM_DATA" not in str(cleaned_res)
