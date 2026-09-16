"""Workspace execution seam (v3.1 §14).

    capability -> workspace -> sandbox -> process

Every capability that runs a process (shell, coding commands, git) funnels
through :func:`run_workspace_command`. There is exactly one place that decides
between the Docker sandbox, the cloud subprocess jail, and the explicitly
opted-in host mode — so no capability can pick a looser execution model, and
no capability ever receives a Docker socket.

Mode is deployment configuration (``tools_shell_mode``):

- ``sandbox`` (default): Docker with CPU/memory/process/output limits.
- ``local``: the host, opt-in only, behind the same approval gate.
- ``off``: no process execution at all.

``heroku_jail=true`` replaces the Docker backend with the in-process
:class:`SubprocessJail` (rlimits, scrubbed env, timeout, output caps) for
platforms without a Docker daemon.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_system.services.tool_errors import ToolError
from agent_system.services.tools.paths import jailed, max_file_bytes, scrub

MAX_OUTPUT_CHARS = 4000


def workspace_dir(ctx: Any, cwd: str | None = None) -> Path:
    """Resolve a working directory inside the allowed roots."""
    requested = cwd or str(getattr(ctx.settings, "workspaces_dir", "workspaces") or "workspaces")
    target = jailed(requested, ctx.settings)
    target.mkdir(parents=True, exist_ok=True)
    if not target.is_dir():
        raise ToolError(f"not a directory: {requested}")
    return target


def shell_mode(ctx: Any) -> str:
    return str(getattr(ctx.settings, "tools_shell_mode", "sandbox") or "sandbox")


def resolve_timeout(ctx: Any, requested: Any) -> int:
    default = int(getattr(ctx.settings, "max_execution_time_seconds", 300) or 300)
    if requested is None:
        return default
    try:
        value = int(requested)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"timeout must be an integer, got {requested!r}") from exc
    if value <= 0:
        raise ToolError("timeout must be positive")
    return min(value, default)


def run_workspace_command(
    ctx: Any,
    command: str,
    *,
    cwd: str | None = None,
    timeout: int | None = None,
) -> dict[str, Any]:
    """Run one command in the workspace. Returns {exit_code, output, mode}."""
    mode = shell_mode(ctx)
    if mode == "off":
        raise ToolError("process execution is disabled (tools_shell_mode=off)")
    workdir = workspace_dir(ctx, cwd)
    seconds = resolve_timeout(ctx, timeout)
    if bool(getattr(ctx.settings, "heroku_jail", False)) or mode == "jail":
        return _run_jail(ctx, str(workdir), command, seconds)
    if mode == "sandbox":
        return _run_sandbox(ctx, str(workdir), command, seconds)
    if mode == "local":
        return _run_local(str(workdir), command, seconds)
    raise ToolError(f"unknown tools_shell_mode '{mode}' (sandbox|jail|local|off)")


def _run_sandbox(ctx: Any, workdir: str, command: str, timeout: int) -> dict[str, Any]:
    try:
        from agent_system.services.sandbox import DockerSandbox
    except Exception as exc:  # pragma: no cover — import-time failure only
        raise ToolError(f"sandbox unavailable: {exc}") from exc
    try:
        outcome = DockerSandbox().run(workdir, command, timeout_seconds=timeout)
    except Exception as exc:
        raise ToolError(f"sandbox exec failed: {exc}") from exc
    return {
        "mode": "sandbox",
        "exit_code": outcome.get("exit_code"),
        "output": scrub(str(outcome.get("stdout", ""))[-MAX_OUTPUT_CHARS:]),
    }


def _run_jail(ctx: Any, workdir: str, command: str, timeout: int) -> dict[str, Any]:
    from agent_system.services.sandbox import SandboxError, SubprocessJail

    allowlist = str(getattr(ctx.settings, "heroku_shell_allowlist", "") or "")
    try:
        outcome = SubprocessJail().run(
            workdir, command, timeout_seconds=timeout, allowlist=allowlist
        )
    except SandboxError as exc:
        raise ToolError(f"jail exec failed: {exc}") from exc
    except Exception as exc:
        raise ToolError(f"jail exec failed: {exc}") from exc
    return {
        "mode": "jail",
        "exit_code": outcome.get("exit_code"),
        "output": scrub(str(outcome.get("stdout", ""))[-MAX_OUTPUT_CHARS:]),
    }


def _run_local(workdir: str, command: str, timeout: int) -> dict[str, Any]:
    import subprocess

    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"command timed out after {timeout}s") from exc
    return {
        "mode": "local",
        "exit_code": proc.returncode,
        "output": scrub(((proc.stdout or "") + (proc.stderr or ""))[-MAX_OUTPUT_CHARS:]),
    }


def read_text(path: Path, ctx: Any, *, limit: int | None = None) -> str:
    """Read a jailed text file with the size cap applied."""
    cap = max_file_bytes(ctx.settings)
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise ToolError(f"no such file: {path.name}") from exc
    except OSError as exc:
        raise ToolError(f"cannot read {path.name}: {exc}") from exc
    if len(data) > cap:
        raise ToolError(f"file exceeds max_file_size_mb ({len(data)} bytes)")
    text = data.decode("utf-8", errors="replace")
    if limit is not None:
        return "\n".join(text.splitlines()[:limit])
    return text


__all__ = [
    "MAX_OUTPUT_CHARS",
    "read_text",
    "resolve_timeout",
    "run_workspace_command",
    "shell_mode",
    "workspace_dir",
]
