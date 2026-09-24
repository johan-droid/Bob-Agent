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


# -- capability registration ------------------------------------------------
#
# This module shipped as dead code: the executor above existed and
# ``/setup ssh`` stored SSH keys in the vault, but nothing ever registered a
# capability, so the agent had no way to run a remote command. Wiring it here
# (execute tier, connection-scoped, approval-gated) makes stored SSH
# credentials actually usable and puts the call on the one execution path
# (``tools/execution.py``) like every other capability.

GROUP = "ssh"


def _ssh_execute(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Run a command on a stored SSH connection (secrets stay in the vault)."""
    from agent_system.services.tool_errors import ToolError

    if ctx.factory is None:
        raise ToolError("ssh_execute requires a database session (credential vault)")
    connection = str(args.get("connection") or "").strip()
    command = str(args.get("command") or "").strip()
    if not connection:
        raise ToolError("ssh_execute: 'connection' is required (e.g. ssh:vps)")
    if not command:
        raise ToolError("ssh_execute: 'command' is required")
    # The vault is scoped to the owning user; SSHService decrypts the key
    # internally and never hands it back to the caller or the model.
    user_id = str(getattr(ctx, "owner_user_id", None) or "operator")
    try:
        result = execute_ssh_command(
            connection,
            command,
            user_id=user_id,
            credential_store=CredentialStore(ctx.factory),
        )
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface as an honest tool error
        raise ToolError(f"ssh_execute failed: {exc}") from exc
    if not result.get("ok"):
        raise ToolError(str(result.get("error") or "ssh command failed"))
    return result


def _scope(args: dict[str, Any]) -> str:
    """Approval is bound to one connection, so approving covers one host.

    Normalizes ``ssh:vps`` and ``vps`` to the same scope so a single approval
    is not re-requested merely because the model wrote the ref differently.
    """
    connection = str(args.get("connection") or "").strip() or "unknown"
    name = connection.split(":", 1)[-1] if ":" in connection else connection
    return f"ssh:{name}"


def register(registry: Any, settings: Any = None) -> None:  # noqa: ARG001
    from agent_system.services.tools.registry import Tool, _str_param

    registry.register(
        Tool(
            name="ssh_execute",
            description=(
                "Run a shell command on a remote host over SSH using a stored "
                "connection (e.g. ssh:vps). The private key never leaves the vault. "
                "Requires approval."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "connection": _str_param("Stored SSH connection ref (e.g. ssh:vps)"),
                    "command": _str_param("Shell command to run on the remote host"),
                },
                "required": ["connection", "command"],
                "additionalProperties": False,
            },
            risk="execute",
            handler=_ssh_execute,
            scope=_scope,
            group=GROUP,
        )
    )


__all__ = ["GROUP", "execute_ssh_command", "register"]
