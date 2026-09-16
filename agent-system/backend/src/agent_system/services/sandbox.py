"""Docker sandbox manager (v3.1 Phase 5) + cloud subprocess jail.

Two backends, one envelope ``{"exit_code", "stdout", "timed_out"}``:

- ``DockerSandbox`` — one container per workspace (local dev, preferred).
  Raises ``SandboxUnavailableError`` when Docker is unreachable — never
  fakes isolation.
- ``SubprocessJail`` — in-process fallback for platforms with no Docker
  daemon (Heroku dynos). Containment only: cwd confined to the workspace,
  secret env stripped, rlimits + timeout + output cap enforced. This is
  NOT container isolation — execute tools stay approval-gated, and an
  allowlist can restrict binaries. Selected via ``HEROKU_JAIL=true``.

Security notes (v3.1 §27):
- containers run with no host filesystem bind-mounts except the workspace dir,
- network is disabled unless explicitly enabled for a task,
- output is capped; execution is time-limited,
- every exec emits `tool.*` events through the caller.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from agent_system.config import get_settings

MAX_OUTPUT_BYTES = 1_000_000

# Cloud jail budgets (Basic 512MB dyno): the jail lives inside the dyno, so
# caps stay well under quota. Process count is NOT limited here on purpose:
# RLIMIT_NPROC counts every process of the uid (breaks shared machines and
# local runs); Heroku Eco/Basic/Standard-1x dynos already enforce a 256
# process/thread ceiling per dyno, which is the backstop in cloud.
JAIL_MAX_AS_BYTES = 256 * 1024 * 1024
JAIL_MAX_CPU_SECONDS = 60

# Env vars never inherited by jailed children (secrets + platform wiring).
# Exact names plus suffix rules (any *_URL/*_URI/*_DSN — provider endpoints,
# database/queue URLs — are never needed by jailed children).
_JAIL_ENV_DENY_EXACT = frozenset({"DATABASE_URL", "REDIS_URL"})
_JAIL_ENV_DENY_SUFFIXES = ("_URL", "_URI", "_DSN")


class SandboxError(RuntimeError):
    pass


class SandboxUnavailableError(SandboxError):
    pass


class DockerSandbox:
    """Runs commands in a per-workspace container with hard limits."""

    IMAGE = "python:3.12-slim"
    # Image with pytest preinstalled for untrusted QA runs (Phase 13).
    QA_IMAGE = "agent-system/qa-sandbox:latest"

    def __init__(self, docker_client: Any = None) -> None:
        if docker_client is None:
            try:
                import docker

                self._client = docker.from_env()
                self._client.ping()
            except Exception as exc:  # docker SDK missing or daemon down
                raise SandboxUnavailableError(f"docker unavailable: {exc}") from exc
        else:
            self._client = docker_client

    def _limits(self) -> dict[str, Any]:
        settings = get_settings()
        return {
            "mem_limit": f"{settings.max_container_memory_mb}m",
            "nano_cpus": int(settings.max_container_cpu * 1e9),
            "pids_limit": 128,
            "network_disabled": True,
        }

    def run(
        self,
        workspace_path: str,
        command: list[str] | str,
        timeout_seconds: int | None = None,
        network: bool = False,
        image: str | None = None,
    ) -> dict[str, Any]:
        """Execute a command inside the sandbox with the workspace mounted at /ws."""
        settings = get_settings()
        timeout = timeout_seconds or settings.max_execution_time_seconds
        if isinstance(command, str):
            command = ["/bin/sh", "-c", command]
        limits = self._limits()
        if network:
            limits.pop("network_disabled")
        # Run as the invoking user so bind-mounted files stay owned by the
        # host user (containers otherwise write root-owned files into /ws).
        uid = os.getuid()
        gid = os.getgid()
        try:
            result = self._client.containers.run(
                image or self.IMAGE,
                command,
                volumes={str(workspace_path): {"bind": "/ws", "mode": "rw"}},
                working_dir="/ws",
                detach=True,
                labels={"agent-system": "sandbox"},
                user=f"{uid}:{gid}",
                **limits,
            )
        except Exception as exc:
            raise SandboxError(f"container start failed: {exc}") from exc
        try:
            outcome = result.wait(timeout=timeout)
            logs = result.logs(stdout=True, stderr=True).decode(errors="replace")
        except Exception as exc:
            result.kill()
            raise SandboxError(f"execution failed or timed out: {exc}") from exc
        finally:
            result.remove(force=True)
        exit_code = int(outcome.get("StatusCode", -1)) if isinstance(outcome, dict) else -1
        return {
            "exit_code": exit_code,
            "stdout": logs[:MAX_OUTPUT_BYTES],
            "timed_out": False,
        }


def quote_args(args: list[str]) -> str:
    """Shell-quote helper so agent-supplied words never break out of quoting."""
    return " ".join(shlex.quote(a) for a in args)


def _scrubbed_env() -> dict[str, str]:
    """Child env minus secrets: key-marker match, deny-list, or URL suffix."""
    from agent_system.services.secrets import is_secret_key

    clean: dict[str, str] = {}
    for key, value in os.environ.items():
        if (
            key in _JAIL_ENV_DENY_EXACT
            or key.endswith(_JAIL_ENV_DENY_SUFFIXES)
            or is_secret_key(key)
        ):
            continue
        clean[key] = value
    return clean


def _jail_preexec() -> None:
    """Apply rlimits in the forked child (POSIX only; best-effort)."""
    try:
        import resource  # noqa: PLC0415 — POSIX-only, imported in child

        resource.setrlimit(resource.RLIMIT_AS, (JAIL_MAX_AS_BYTES, JAIL_MAX_AS_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (JAIL_MAX_CPU_SECONDS, JAIL_MAX_CPU_SECONDS))
    except Exception:
        pass


def check_allowlist(command: str, allowlist: str) -> None:
    """Enforce an optional comma-separated binary-prefix allowlist.

    Empty allowlist = no restriction (approval gate still applies).
    Otherwise the command must start with one of the prefixes, else
    ``SandboxError``. Best-effort for shell strings — the approval gate
    remains the primary control.
    """
    prefixes = [p.strip() for p in (allowlist or "").split(",") if p.strip()]
    if not prefixes:
        return
    if not any(command.strip().startswith(p) for p in prefixes):
        raise SandboxError(
            f"command not in allowlist (must start with one of: {', '.join(prefixes)})"
        )


class SubprocessJail:
    """Contained subprocess execution for Docker-less platforms (Heroku).

    Mirrors the ``DockerSandbox.run`` envelope so callers can swap backends.
    cwd is resolved and confined to ``workspace_path``; secrets are stripped
    from the child env; rlimits, timeout, and output caps are enforced.
    Containment, NOT isolation — see module docstring.
    """

    def run(
        self,
        workspace_path: str,
        command: list[str] | str,
        timeout_seconds: int | None = None,
        allowlist: str = "",
    ) -> dict[str, Any]:
        settings = get_settings()
        timeout = timeout_seconds or settings.max_execution_time_seconds
        if isinstance(command, list):
            command = quote_args(command)
        check_allowlist(command, allowlist)
        ws = Path(workspace_path).expanduser()
        if not ws.is_absolute():
            ws = Path.cwd() / ws
        try:
            resolved = ws.resolve()
        except (OSError, ValueError) as exc:
            raise SandboxError(f"unresolvable workspace: {workspace_path}") from exc
        resolved.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.run(
                ["/bin/sh", "-c", command],
                cwd=str(resolved),
                env=_scrubbed_env(),
                capture_output=True,
                text=True,
                timeout=timeout,
                preexec_fn=_jail_preexec,
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxError(f"execution timed out after {timeout}s") from exc
        except OSError as exc:
            raise SandboxError(f"execution failed: {exc}") from exc
        logs = (proc.stdout or "") + (proc.stderr or "")
        return {
            "exit_code": proc.returncode,
            "stdout": logs[:MAX_OUTPUT_BYTES],
            "timed_out": False,
        }
