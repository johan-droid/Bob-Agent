"""System environment detection for setup/doctor/bootstrap.

Stdlib only — safe to import anywhere (the standalone ``bootstrap.py``
installer vendors a trimmed copy of the probing logic). No network calls,
no side effects; every probe is best-effort and degrades to ``None``.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass, field

#: Minimum supported interpreter for the backend (matches pyproject).
MIN_PYTHON = (3, 12)

#: Ports the stack cares about: API server.
CHECK_PORTS = (8000,)

#: Version flags tried per tool (first one that works wins).
_VERSION_FLAGS = ("--version", "-V", "-v", "version")


@dataclass(frozen=True)
class ToolStatus:
    """One probed executable."""

    name: str
    found: bool
    version: str | None = None
    path: str | None = None
    hint: str | None = None


@dataclass(frozen=True)
class SystemReport:
    """Everything the installer/wizard needs to adapt to this machine."""

    os: str
    distro: str
    arch: str
    cpu_count: int
    ram_gb: float | None
    disk_free_gb: float
    python_version: str
    python_ok: bool
    tools: dict[str, ToolStatus] = field(default_factory=dict)
    ports_in_use: dict[int, bool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable snapshot (for ``agentctl doctor --json``)."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Probes (all best-effort)
# ---------------------------------------------------------------------------


def _distro() -> str:
    if sys.platform == "darwin":
        return f"macOS {platform.mac_ver()[0]}".strip()
    if sys.platform == "win32":
        return f"Windows {platform.version()}"
    try:
        text = open("/etc/os-release", encoding="utf-8").read()
        match = re.search(r'^PRETTY_NAME="?(.*?)"?\s*$', text, re.M)
        if match:
            return match.group(1)
    except OSError:
        pass
    return platform.platform()


def _ram_gb() -> float | None:
    try:
        if sys.platform == "linux":
            with open("/proc/meminfo", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return round(kb / 1024 / 1024, 1)
        elif sys.platform == "darwin":
            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5
            )
            if out.returncode == 0:
                return round(int(out.stdout.strip()) / 1024**3, 1)
    except Exception:
        pass
    return None


def _tool_version(exe: str) -> str | None:
    for flag in _VERSION_FLAGS:
        try:
            out = subprocess.run([exe, flag], capture_output=True, text=True, timeout=8)
            if out.returncode == 0:
                text = (out.stdout or out.stderr).strip().splitlines()
                if text:
                    return text[0][:80]
        except Exception:
            continue
    return None


def _probe_tool(cmd: str, hint: str) -> ToolStatus:
    path = shutil.which(cmd)
    if path is None:
        return ToolStatus(name=cmd, found=False, hint=hint)
    return ToolStatus(name=cmd, found=True, version=_tool_version(path), path=path)


def probe_tools() -> dict[str, ToolStatus]:
    """Probe the executables the stack needs (uv/git/docker)."""
    tools = {
        "uv": _probe_tool("uv", "Install uv: https://docs.astral.sh/uv/#installation"),
        "git": _probe_tool("git", "Install git from https://git-scm.com/downloads"),
        "docker": _probe_tool("docker", "Install Docker for sandbox isolation (optional)"),
    }
    compose = shutil.which("docker-compose")
    compose_plugin = False
    if tools["docker"].found:
        try:
            out = subprocess.run(
                ["docker", "compose", "version"], capture_output=True, text=True, timeout=10
            )
            compose_plugin = out.returncode == 0
        except Exception:
            compose_plugin = False
    hint = None if (compose or compose_plugin) else "Needs `docker compose` (plugin or standalone)"
    tools["docker-compose"] = ToolStatus(
        name="docker-compose",
        found=bool(compose or compose_plugin),
        version=_tool_version(compose) if compose else ("plugin" if compose_plugin else None),
        path=compose,
        hint=hint,
    )
    return tools


def _port_in_use(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        return sock.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def detect_environment() -> SystemReport:
    """Probe this machine and summarize what setup must do."""
    py = sys.version_info
    python_version = f"{py.major}.{py.minor}.{py.micro}"
    python_ok = (py.major, py.minor) >= MIN_PYTHON
    tools = probe_tools()
    ports = {p: _port_in_use(p) for p in CHECK_PORTS}

    warnings: list[str] = []
    if not python_ok:
        warnings.append(f"Python {python_version} < 3.12 — install Python 3.12+ before continuing.")
    if not tools["uv"].found:
        warnings.append("uv not found — the installer can fetch it automatically.")
    if not tools["git"].found:
        warnings.append("git not found — needed to clone the repo / import skills from git.")
    if ports.get(8000):
        warnings.append("Port 8000 is already in use — `make start` may fail to bind.")

    disk_free = round(shutil.disk_usage(os.getcwd()).free / 1024**3, 1)
    return SystemReport(
        os=platform.system(),
        distro=_distro(),
        arch=platform.machine(),
        cpu_count=os.cpu_count() or 1,
        ram_gb=_ram_gb(),
        disk_free_gb=disk_free,
        python_version=python_version,
        python_ok=python_ok,
        tools=tools,
        ports_in_use=ports,
        warnings=warnings,
    )


def render_report(report: SystemReport) -> None:
    """Pretty-print the report with Rich (imported lazily)."""
    from rich import box
    from rich.console import Console
    from rich.table import Table

    console = Console()
    table = Table(box=box.SIMPLE_HEAVY, title="[bold]Environment[/bold]", title_justify="left")
    table.add_column("Check", style="bold")
    table.add_column("Result")
    table.add_row("OS", f"{report.os} ({report.distro}) · {report.arch} · {report.cpu_count} CPU")
    ram = f"{report.ram_gb} GB" if report.ram_gb is not None else "unknown"
    table.add_row("Memory / Disk free", f"{ram} / {report.disk_free_gb} GB")
    py_mark = "✓" if report.python_ok else "✗"
    table.add_row(
        "Python",
        f"[{('green' if report.python_ok else 'red')}]{py_mark}[/] {report.python_version}",
    )
    for tool in ("uv", "git", "docker", "docker-compose"):
        status = report.tools[tool]
        if status.found:
            detail = status.version or "found"
            table.add_row(tool, f"[green]✓[/] {detail}")
        else:
            table.add_row(tool, f"[dim]– missing[/dim] [dim]({status.hint})[/dim]")
    for port, busy in report.ports_in_use.items():
        table.add_row(f"Port {port}", "[yellow]in use[/]" if busy else "[green]free[/]")
    console.print(table)
    for warning in report.warnings:
        console.print(f"[yellow]! {warning}[/yellow]")
