"""Unit tests for SSHService and SSH tool execution using connection references."""

import pytest

from agent_system.infra.db import make_engine, make_session_factory
from agent_system.infra.models import Base, User
from agent_system.services.credentials import CredentialStore
from agent_system.services.ssh_service import SSHService
from agent_system.services.tools.builtin.ssh import execute_ssh_command


@pytest.fixture
def db_factory(tmp_path):
    db_file = tmp_path / "test_ssh.db"
    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        session.add(
            User(id="usr_01", display_name="User 1", auth_provider="telegram", role="admin")
        )
        session.commit()
    return factory


def test_ssh_execution_with_credential_reference(db_factory):
    vault = CredentialStore(db_factory)
    vault.save(
        "usr_01",
        "ssh",
        "home-server",
        {
            "hostname": "192.168.1.50",
            "port": 22,
            "username": "ubuntu",
            "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\nsome_key\n-----END OPENSSH PRIVATE KEY-----",
            "trust_on_first_use": True,
        },
    )

    ssh_svc = SSHService(vault)
    res = ssh_svc.execute_command("usr_01", "home-server", "uname -a")
    assert res.exit_code == 0
    assert "192.168.1.50" in res.stdout
    assert res.host_verified is True
    assert res.error is None


def test_ssh_untrusted_host_requires_confirmation(db_factory):
    vault = CredentialStore(db_factory)
    vault.save(
        "usr_01",
        "ssh",
        "vps",
        {
            "hostname": "myvps.example.com",
            "port": 2222,
            "username": "root",
            "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\nkey\n-----END OPENSSH PRIVATE KEY-----",
            "trust_on_first_use": False,
        },
    )

    ssh_svc = SSHService(vault)
    res = ssh_svc.execute_command("usr_01", "vps", "df -h")
    assert res.exit_code == 1
    assert res.host_verified is False
    assert "Host key verification failed" in res.error

    fp = ssh_svc.get_host_fingerprint("myvps.example.com", 2222)
    ssh_svc.trust_host_fingerprint("myvps.example.com", 2222, fp.fingerprint_sha256)

    res_after = ssh_svc.execute_command("usr_01", "vps", "df -h")
    assert res_after.exit_code == 0
    assert res_after.host_verified is True


def test_ssh_builtin_tool_wrapper(db_factory):
    vault = CredentialStore(db_factory)
    vault.save(
        "usr_01",
        "ssh",
        "raspberry-pi",
        {
            "hostname": "raspberrypi.local",
            "username": "pi",
            "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\nkey\n-----END OPENSSH PRIVATE KEY-----",
            "trust_on_first_use": True,
        },
    )

    result = execute_ssh_command(
        connection="ssh:raspberry-pi",
        command="uptime",
        user_id="usr_01",
        credential_store=vault,
    )
    assert result["ok"] is True
    assert result["connection"] == "raspberry-pi"
    assert "uptime" in result["stdout"]
