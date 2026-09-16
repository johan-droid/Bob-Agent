"""Shell capability (v3.1 §14).

Arbitrary shell remains available — but as one explicitly declared, explicitly
gated capability whose containment is decided in one place
(``builtin/_exec.py``), never per call site.

Risk ``execute``: every invocation needs a live approval unless the deployment
sets ``tools_require_approval=false``. Default execution is the Docker sandbox;
the host is only reachable with an explicit ``tools_shell_mode=local`` opt-in.
"""

from __future__ import annotations

from typing import Any

from agent_system.services.tool_errors import ToolError
from agent_system.services.tools.builtin._exec import run_workspace_command
from agent_system.services.tools.registry import Tool, ToolContext, ToolRegistry, _str_param

GROUP = "shell"


def _shell(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    command = str(args.get("command") or "").strip()
    if not command:
        raise ToolError("shell: 'command' is required")
    cwd = str(args.get("cwd") or "") or None
    return run_workspace_command(ctx, command, cwd=cwd, timeout=args.get("timeout"))


def _scope(args: dict[str, Any]) -> str:
    """Approval is bound to the command, so approving one run approves one run."""
    command = " ".join(str(args.get("command") or "").split())[:60]
    return f"shell:{command}" if command else "shell"


def register(registry: ToolRegistry, settings: Any = None) -> None:  # noqa: ARG001
    registry.register(
        Tool(
            name="shell",
            description=(
                "Run a shell command in the workspace sandbox. "
                "Requires approval; tools_shell_mode=off disables it entirely."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": _str_param("Shell command to run"),
                    "cwd": _str_param("Working directory inside the jail (default workspaces)"),
                    "timeout": {"type": "integer", "minimum": 1, "description": "Timeout seconds"},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            risk="execute",
            handler=_shell,
            scope=_scope,
            group=GROUP,
        )
    )


__all__ = ["GROUP", "register"]
