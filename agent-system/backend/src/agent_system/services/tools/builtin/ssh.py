"""Built-in SSH tool using connection references (Chat-Native Architecture §6 & §20)."""

from __future__ import annotations

from typing import Any

from agent_system.services.credentials import CredentialStore
from agent_system.services.ssh_service import SSHService


def execute_ssh_command(
    connection: str,
    command: str,
    *,
    user_id: str = "operator",
    credential_store: CredentialStore | None = None,
    ssh_service: SSHService | None = None,
) -> dict[str, Any]:
    """Execute a command on a configured remote SSH host by connection reference name."""
    if not connection or not command:
        return {"ok": False, "error": "Both 'connection' and 'command' parameters are required."}

    conn_name = connection.split(":", 1)[-1] if ":" in connection else connection

    if ssh_service is None:
        if credential_store is None:
            return {"ok": False, "error": "CredentialStore or SSHService must be provided."}
        ssh_service = SSHService(credential_store)

    res = ssh_service.execute_command(user_id, conn_name, command)
    if res.error:
        return {
            "ok": False,
            "connection": conn_name,
            "exit_code": res.exit_code,
            "error": res.error,
            "host_verified": res.host_verified,
        }

    return {
        "ok": True,
        "connection": conn_name,
        "exit_code": res.exit_code,
        "stdout": res.stdout,
        "stderr": res.stderr,
        "host_verified": res.host_verified,
    }
