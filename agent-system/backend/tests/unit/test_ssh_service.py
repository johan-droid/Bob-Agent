"""Unit tests for SSHService and SSH tool execution using connection references.

These use a fake paramiko transport rather than a real host. They previously
dialed a hardcoded LAN address (`192.168.1.50`) and a `.local` mDNS name, so
they passed only on the machine of whoever wrote them and failed in CI. The
behaviour under test — credential-reference resolution, host-key trust
decisions, and the tool wrapper — is all local logic and needs no network.
"""

from typing import Any
from unittest.mock import patch

import pytest

from agent_system.infra.db import make_engine, make_session_factory
from agent_system.infra.models import Base, User
from agent_system.services.credentials import CredentialStore
from agent_system.services.ssh_service import SSHService
from agent_system.services.tools.builtin.ssh import execute_ssh_command


class FakeHostKey:
    """paramiko.PKey stand-in: the service hashes ``asbytes()`` and names it."""

    def asbytes(self) -> bytes:
        return b"SSH-DUMMY-KEY"

    def get_name(self) -> str:
        return "ssh-ed25519"


HOST_KEY = FakeHostKey()


class FakeStream:
    def __init__(self, data: bytes = b"") -> None:
        self._data = data
        self.channel = self

    def read(self) -> bytes:
        return self._data

    def recv_exit_status(self) -> int:
        return 0

    def close(self) -> None:
        return None


class FakeTransport:
    @staticmethod
    def is_active() -> bool:
        return True

    @staticmethod
    def get_remote_server_key() -> bytes:
        return HOST_KEY


class FakeTransportSession:
    """paramiko.Transport stand-in for get_host_fingerprint's probe."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        self.banner_timeout = 0
        self.handshake_timeout = 0

    def start_client(self, timeout: float = 0) -> None:
        return None

    def get_remote_server_key(self) -> bytes:
        return HOST_KEY

    def close(self) -> None:
        return None


def fake_transport_patch():
    """Patch both the socket probe and paramiko.Transport used for fingerprinting."""
    return (
        patch("socket.create_connection", lambda *a, **kw: _NullConn()),
        patch("paramiko.Transport", FakeTransportSession),
    )


class _NullConn:
    def __enter__(self) -> "_NullConn":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def close(self) -> None:
        return None


class FakeSSHClient:
    """Minimal paramiko.SSHClient stand-in.

    ``connect`` raises when the caller asked for rejection, which is how the
    untrusted-host case is driven; the host key is always the same, so a
    previously trusted fingerprint keeps verifying.
    """

    def __init__(self, refuse: bool = False, banner: bytes = b"") -> None:
        self._refuse = refuse
        self._banner = banner
        self.exec_calls: list[str] = []

    def set_missing_host_key_policy(self, policy: Any) -> None:
        return None

    def get_transport(self) -> Any:
        return FakeTransport()

    def connect(self, *a: Any, **kw: Any) -> None:
        if self._refuse:
            raise OSError("Host key verification failed")

    def exec_command(self, cmd: str, *a: Any, **kw: Any) -> Any:
        self.exec_calls.append(cmd)
        return (FakeStream(b""), FakeStream(self._banner), FakeStream(b""))

    def close(self) -> None:
        return None


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
            "hostname": "example.invalid",
            "port": 22,
            "username": "ubuntu",
            "private_key": (
                "-----BEGIN OPENSSH PRIVATE KEY-----\nsome_key\n-----END OPENSSH PRIVATE KEY-----"
            ),
            "trust_on_first_use": True,
        },
    )

    ssh_svc = SSHService(vault)
    sock, transport = fake_transport_patch()
    with (
        sock,
        transport,
        patch("paramiko.SSHClient", lambda: FakeSSHClient(banner=b"uname -a output\n")),
    ):
        res = ssh_svc.execute_command("usr_01", "home-server", "uname -a")
    assert res.exit_code == 0, res.error
    assert "uname -a output" in res.stdout
    assert res.host_verified is True
    assert res.error is None


def test_ssh_untrusted_host_requires_confirmation(db_factory):
    vault = CredentialStore(db_factory)
    vault.save(
        "usr_01",
        "ssh",
        "vps",
        {
            "hostname": "vps.example.invalid",
            "port": 2222,
            "username": "root",
            "private_key": (
                "-----BEGIN OPENSSH PRIVATE KEY-----\nkey\n-----END OPENSSH PRIVATE KEY-----"
            ),
            "trust_on_first_use": False,
        },
    )

    ssh_svc = SSHService(vault)
    sock, transport = fake_transport_patch()
    # Untrusted host: the service must refuse, not silently run the command.
    with sock, transport, patch("paramiko.SSHClient", lambda: FakeSSHClient()):
        res = ssh_svc.execute_command("usr_01", "vps", "df -h")
    assert res.exit_code == 1
    assert res.host_verified is False
    assert res.error and "verification failed" in res.error.lower()

    # After the operator trusts the fingerprint, the same host runs.
    with sock, transport, patch("paramiko.SSHClient", lambda: FakeSSHClient(banner=b"df output\n")):
        fp = ssh_svc.get_host_fingerprint("vps.example.invalid", 2222)
        assert fp is not None
        ssh_svc.trust_host_fingerprint("vps.example.invalid", 2222, fp.fingerprint_sha256)
        res_after = ssh_svc.execute_command("usr_01", "vps", "df -h")
    assert res_after.exit_code == 0, res_after.error
    assert res_after.host_verified is True


def test_ssh_builtin_tool_wrapper(db_factory):
    vault = CredentialStore(db_factory)
    vault.save(
        "usr_01",
        "ssh",
        "raspberry-pi",
        {
            "hostname": "raspberrypi.example.invalid",
            "username": "pi",
            "private_key": (
                "-----BEGIN OPENSSH PRIVATE KEY-----\nkey\n-----END OPENSSH PRIVATE KEY-----"
            ),
            "trust_on_first_use": True,
        },
    )

    sock, transport = fake_transport_patch()
    with (
        sock,
        transport,
        patch("paramiko.SSHClient", lambda: FakeSSHClient(banner=b"uptime output\n")),
    ):
        result = execute_ssh_command(
            connection="ssh:raspberry-pi",
            command="uptime",
            user_id="usr_01",
            credential_store=vault,
        )
    assert result["ok"] is True, result
    assert result["connection"] == "raspberry-pi"
    assert "uptime output" in result["stdout"]


def test_unverifiable_host_fails_closed(db_factory):
    """A failed fingerprint probe must abort, not run the command.

    get_host_fingerprint documents that None means "unverifiable" and that
    callers must abort. The guard read `if verify_host and fp:`, so a probe
    that failed (transient network, or a MITM swallowing it) skipped
    verification and executed the command with host_verified=True.
    """
    vault = CredentialStore(db_factory)
    vault.save(
        "usr_01",
        "ssh",
        "vps",
        {
            "hostname": "vps.example.invalid",
            "port": 2222,
            "username": "root",
            "private_key": (
                "-----BEGIN OPENSSH PRIVATE KEY-----\nkey\n-----END OPENSSH PRIVATE KEY-----"
            ),
            "trust_on_first_use": True,
        },
    )
    ssh_svc = SSHService(vault)
    # Probe fails (no socket, transport raises) -> fingerprint is None.
    with (
        patch("socket.create_connection", side_effect=OSError("probe blocked")),
        patch("paramiko.Transport", side_effect=OSError("probe blocked")),
        patch("paramiko.SSHClient") as client,
    ):
        res = ssh_svc.execute_command("usr_01", "vps", "id")
    assert res.exit_code == 1
    assert res.host_verified is False
    assert res.error and "could not be verified" in res.error.lower()
    client.assert_not_called()  # never connected
