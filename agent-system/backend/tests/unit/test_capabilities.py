"""Unit tests for CapabilityRegistry and provider capability resolution."""

import pytest

from agent_system.infra.db import make_engine, make_session_factory
from agent_system.infra.models import Base, User
from agent_system.services.capabilities import CapabilityRegistry
from agent_system.services.credentials import CredentialStore


@pytest.fixture
def db_factory(tmp_path):
    db_file = tmp_path / "test_caps.db"
    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        session.add(
            User(id="usr_01", display_name="User 1", auth_provider="telegram", role="admin")
        )
        session.commit()
    return factory


def test_capability_resolution_for_user(db_factory):
    vault = CredentialStore(db_factory)
    vault.save("usr_01", "github", "personal", {"token": "ghp_12345"})
    vault.save("usr_01", "ssh", "home-server", {"hostname": "10.0.0.1", "private_key": "..."})

    registry = CapabilityRegistry(vault)
    caps = registry.get_user_capabilities("usr_01")

    assert len(caps) == 2
    refs = {c.connection_ref for c in caps}
    assert refs == {"github:personal", "ssh:home-server"}

    gh_cap = next(c for c in caps if c.connection_ref == "github:personal")
    assert gh_cap.credential_type == "oauth"
    assert "git_clone" in gh_cap.authorized_tools
    assert any(cap.name == "repos" for cap in gh_cap.capabilities)


def test_tool_authorization_check(db_factory):
    vault = CredentialStore(db_factory)
    vault.save("usr_01", "ssh", "vps", {"hostname": "1.2.3.4", "private_key": "..."})

    registry = CapabilityRegistry(vault)
    assert registry.is_tool_authorized("usr_01", "ssh:vps", "ssh_execute") is True
    assert registry.is_tool_authorized("usr_01", "ssh:vps", "unknown_tool") is False

    # Revoked connection loses tool authorization
    vault.revoke("usr_01", "ssh", "vps")
    assert registry.is_tool_authorized("usr_01", "ssh:vps", "ssh_execute") is False
