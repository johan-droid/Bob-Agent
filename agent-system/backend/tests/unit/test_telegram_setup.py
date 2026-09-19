"""Unit tests for interactive Telegram setup commands and credential management."""

import pytest

from agent_system.config import Settings
from agent_system.infra.db import make_engine, make_session_factory
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Base, User
from agent_system.services.credentials import CredentialStore
from agent_system.services.identity import IdentityMode, Principal, Role
from agent_system.services.permissions import PermissionGate
from agent_system.services.telegram import _ACTIVE_SETUPS, TelegramService


@pytest.fixture
def db_factory(tmp_path):
    db_file = tmp_path / "test_tg_setup.db"
    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        session.add(User(id="usr_01", display_name="Operator", auth_provider="telegram", role="admin"))
        session.commit()
    return factory


@pytest.mark.asyncio
async def test_interactive_ssh_setup_workflow(db_factory):
    svc = TelegramService(settings=Settings(), session_factory=db_factory, gate=PermissionGate(), bus=EventBus())
    principal = Principal(user_id="usr_01", role=Role.ADMIN, mode=IdentityMode.TELEGRAM)
    chat_id = 123456

    # 1. Start setup
    await svc._dispatch_command(principal, chat_id, "/setup ssh")
    assert chat_id in _ACTIVE_SETUPS
    assert _ACTIVE_SETUPS[chat_id].step == "ask_name"

    # 2. Supply name
    handled = await svc._handle_active_setup_step(principal, chat_id, "home-server")
    assert handled is True
    assert _ACTIVE_SETUPS[chat_id].step == "ask_host"

    # 3. Supply host
    handled = await svc._handle_active_setup_step(principal, chat_id, "10.0.0.5")
    assert handled is True
    assert _ACTIVE_SETUPS[chat_id].step == "ask_user"

    # 4. Supply user
    handled = await svc._handle_active_setup_step(principal, chat_id, "ubuntu")
    assert handled is True
    assert _ACTIVE_SETUPS[chat_id].step == "ask_key"

    # 5. Supply private key (secret input)
    pem_key = "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret_key_bytes\n-----END OPENSSH PRIVATE KEY-----"
    handled = await svc._handle_active_setup_step(principal, chat_id, pem_key)
    assert handled is True
    assert chat_id not in _ACTIVE_SETUPS  # Session completed and purged

    # Verify secret stored in CredentialStore
    vault = CredentialStore(db_factory)
    payload = vault.get("usr_01", "ssh", "home-server")
    assert payload is not None
    assert payload["hostname"] == "10.0.0.5"
    assert payload["username"] == "ubuntu"
    assert payload["private_key"] == pem_key


@pytest.mark.asyncio
async def test_interactive_api_key_setup_workflow(db_factory):
    svc = TelegramService(settings=Settings(), session_factory=db_factory, gate=PermissionGate(), bus=EventBus())
    principal = Principal(user_id="usr_01", role=Role.ADMIN, mode=IdentityMode.TELEGRAM)
    chat_id = 654321

    await svc._dispatch_command(principal, chat_id, "/setup groq personal")
    assert _ACTIVE_SETUPS[chat_id].step == "ask_api_key"

    handled = await svc._handle_active_setup_step(principal, chat_id, "gsk_test_api_key_789")
    assert handled is True
    assert chat_id not in _ACTIVE_SETUPS

    vault = CredentialStore(db_factory)
    payload = vault.get("usr_01", "groq", "personal")
    assert payload == {"api_key": "gsk_test_api_key_789"}


@pytest.mark.asyncio
async def test_connection_management_commands(db_factory):
    svc = TelegramService(settings=Settings(), session_factory=db_factory, gate=PermissionGate(), bus=EventBus())
    principal = Principal(user_id="usr_01", role=Role.ADMIN, mode=IdentityMode.TELEGRAM)
    chat_id = 999999

    vault = CredentialStore(db_factory)
    vault.save("usr_01", "github", "personal", {"token": "ghp_12345"})

    # Test /connections
    await svc._dispatch_command(principal, chat_id, "/connections")

    # Test /revoke
    await svc._dispatch_command(principal, chat_id, "/revoke github:personal")
    assert vault.get("usr_01", "github", "personal") is None

    # Test /remove
    await svc._dispatch_command(principal, chat_id, "/remove github:personal")
    assert len(vault.list_metadata("usr_01")) == 0
