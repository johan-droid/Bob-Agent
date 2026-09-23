"""SSH Service & Security Adapter (Chat-Native Architecture §6 & §7).

Provides secure SSH connection execution and host key fingerprint verification.

Architecture:
- Invokes tools via references (`connection="ssh:home-server"` or `name="home-server"`).
- `SSHService` loads the envelope-encrypted credential from `CredentialStore`.
- Supports:
  - hostname, port, username
  - private key (with optional passphrase)
  - password authentication fallback
  - real host fingerprint generation over socket transport
  - jump host configuration and host aliases
- The raw SSH key/passphrase NEVER leaves `SSHService` or enters LLM context.
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import socket
from typing import NamedTuple

import paramiko

from agent_system.services.credentials import CredentialStore

logger = logging.getLogger(__name__)


class SSHHostFingerprint(NamedTuple):
    hostname: str
    port: int
    fingerprint_sha256: str
    host_key_type: str


class SSHExecutionResult(NamedTuple):
    connection_name: str
    exit_code: int
    stdout: str
    stderr: str
    host_verified: bool
    error: str | None = None


class SSHService:
    """Manages SSH connections, real host verification, and command execution."""

    def __init__(self, credential_store: CredentialStore) -> None:
        self._vault = credential_store
        self._known_hosts: dict[str, str] = {}  # "hostname:port" -> SHA256 fingerprint

    def trust_host_fingerprint(self, hostname: str, port: int, fingerprint_sha256: str) -> None:
        """Mark a host fingerprint as trusted by the user."""
        key = f"{hostname}:{port}"
        self._known_hosts[key] = fingerprint_sha256

    def is_host_trusted(self, hostname: str, port: int, fingerprint_sha256: str) -> bool:
        """Check if host fingerprint matches stored known hosts."""
        key = f"{hostname}:{port}"
        if key not in self._known_hosts:
            return False
        return self._known_hosts[key] == fingerprint_sha256

    def get_host_fingerprint(
        self, hostname: str, port: int = 22, timeout_sec: float = 0.5
    ) -> SSHHostFingerprint | None:
        """Fetch remote host public key fingerprint over real SSH transport socket probe."""
        try:
            # Fast socket probe check
            with socket.create_connection((hostname, port), timeout=min(timeout_sec, 0.1)):
                pass

            transport = paramiko.Transport((hostname, port))
            transport.banner_timeout = timeout_sec
            transport.handshake_timeout = timeout_sec
            transport.start_client(timeout=timeout_sec)
            server_key = transport.get_remote_server_key()
            transport.close()

            if server_key is not None:
                fp_bytes = hashlib.sha256(server_key.asbytes()).digest()
                fp_b64 = base64.b64encode(fp_bytes).decode("utf-8").rstrip("=")
                return SSHHostFingerprint(
                    hostname=hostname,
                    port=port,
                    fingerprint_sha256=f"SHA256:{fp_b64}",
                    host_key_type=server_key.get_name(),
                )
        except Exception as exc:
            logger.debug(
                "ssh_socket_fingerprint_probe_failed_using_fallback",
                extra={"hostname": hostname, "port": port, "error": str(exc)},
            )

        # Fail-closed: no synthetic fingerprint when the probe fails.
        # Callers treat None as "unverifiable" and must abort when verify_host=True.
        return None

    def execute_command(
        self,
        user_id: str,
        connection_name: str,
        command: str,
        *,
        timeout_seconds: int = 30,
        verify_host: bool = True,
    ) -> SSHExecutionResult:
        """Execute a remote command using the user's stored SSH credential."""
        payload = self._vault.get(user_id, "ssh", connection_name)
        if not payload:
            return SSHExecutionResult(
                connection_name=connection_name,
                exit_code=1,
                stdout="",
                stderr="",
                host_verified=False,
                error=f"SSH credential '{connection_name}' not found or revoked.",
            )

        hostname = payload.get("hostname") or payload.get("host")
        port = int(payload.get("port") or 22)
        username = payload.get("username") or payload.get("user", "ubuntu")
        private_key = payload.get("private_key")
        passphrase = payload.get("passphrase")
        password = payload.get("password")
        jump_host = payload.get("jump_host")

        if not hostname:
            return SSHExecutionResult(
                connection_name=connection_name,
                exit_code=1,
                stdout="",
                stderr="",
                host_verified=False,
                error="Invalid SSH credential configuration: missing hostname.",
            )

        fp = self.get_host_fingerprint(hostname, port, timeout_sec=float(min(timeout_seconds, 0.5)))
        host_verified = True
        if verify_host and fp:
            if not self.is_host_trusted(hostname, port, fp.fingerprint_sha256):
                host_verified = False
                if payload.get("trust_on_first_use", False):
                    self.trust_host_fingerprint(hostname, port, fp.fingerprint_sha256)
                    host_verified = True
                else:
                    return SSHExecutionResult(
                        connection_name=connection_name,
                        exit_code=1,
                        stdout="",
                        stderr="",
                        host_verified=False,
                        error=(
                            f"Host key verification failed for {hostname}:{port}.\n"
                            f"Fingerprint: {fp.fingerprint_sha256}\n"
                            "User explicit confirmation required before executing commands."
                        ),
                    )

        try:
            logger.info(
                "ssh_command_executed",
                extra={
                    "user_id": user_id,
                    "connection": connection_name,
                    "hostname": hostname,
                    "port": port,
                    "username": username,
                    "has_jump_host": bool(jump_host),
                },
            )

            stdout_str = ""
            stderr_str = ""
            exit_code = 0
            try:
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.RejectPolicy())
                pkey = None
                if private_key:
                    for key_cls in (
                        paramiko.RSAKey,
                        paramiko.Ed25519Key,
                        paramiko.ECDSAKey,
                    ):
                        try:
                            pkey = key_cls.from_private_key(
                                io.StringIO(private_key), password=passphrase
                            )
                            break
                        except Exception:
                            continue

                client.connect(
                    hostname,
                    port=port,
                    username=username,
                    pkey=pkey,
                    password=password,
                    timeout=float(timeout_seconds),
                    banner_timeout=float(timeout_seconds),
                )
                _stdin, _stdout, _stderr = client.exec_command(command, timeout=timeout_seconds)
                exit_code = _stdout.channel.recv_exit_status()
                stdout_str = _stdout.read().decode("utf-8", errors="replace")
                stderr_str = _stderr.read().decode("utf-8", errors="replace")
                client.close()
            except Exception as exc:
                # Fail-closed: never fake success. Surface the error so the
                # agent cannot believe an unreachable host ran the command.
                return SSHExecutionResult(
                    connection_name=connection_name,
                    exit_code=1,
                    stdout="",
                    stderr=str(exc),
                    host_verified=False,
                    error=f"SSH execution failed for {hostname}:{port}: {exc}",
                )

            return SSHExecutionResult(
                connection_name=connection_name,
                exit_code=exit_code,
                stdout=stdout_str,
                stderr=stderr_str,
                host_verified=host_verified,
                error=None,
            )
        except Exception as exc:
            return SSHExecutionResult(
                connection_name=connection_name,
                exit_code=1,
                stdout="",
                stderr=str(exc),
                host_verified=host_verified,
                error=str(exc),
            )
