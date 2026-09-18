#!/usr/bin/env python3
"""Bob Agent one-shot bootstrapper — stdlib only, Windows/Linux/macOS.

Detects the machine, installs what's missing (Python 3.12+, uv, git,
Redis-via-Docker), fetches the repo, then hands off to ``agentctl setup``
which remembers your answers for next time.

Run from a checkout (no network needed)::

    python3 bootstrap/bootstrap.py [--yes] [--skip-setup] [--no-docker]

Or as a one-liner once published (override the base with BOB_REPO_URL)::

    curl -fsSL <BASE>/bootstrap/install.sh | bash
    irm <BASE>/bootstrap/install.ps1 | iex
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request

DEFAULT_REPO_URL = os.environ.get(
    "BOB_REPO_URL", "https://github.com/johan-droid/Bob-Agent.git"
)
MIN_PYTHON = (3, 12)


def log(msg: str) -> None:
    print(f"[bootstrap] {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"[bootstrap] ! {msg}", flush=True)


def run(cmd: list[str], cwd: str | None = None, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)


def ask(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        log(f"{question} [auto-yes]")
        return True
    try:
        answer = input(f"[bootstrap] {question} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


# ---------------------------------------------------------------------------
# Detection (trimmed stdlib copy of backend sysdetect logic)
# ---------------------------------------------------------------------------


def get_platform_group() -> str:
    """Return 'windows', 'macos', or 'linux' for platform-specific logic."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def detect_package_manager() -> str | None:
    """Detect the system's package manager."""
    if sys.platform == "darwin":
        if shutil.which("brew"):
            return "brew"
        if shutil.which("port"):
            return "macports"
        return None
    if sys.platform == "win32":
        if shutil.which("winget"):
            return "winget"
        if shutil.which("choco"):
            return "chocolatey"
        if shutil.which("scoop"):
            return "scoop"
        return None
    # Linux — check for package managers in order of preference
    for pm, cmd in [
        ("apt", "apt-get"),
        ("dnf", "dnf"),
        ("yum", "yum"),
        ("pacman", "pacman"),
        ("zypper", "zypper"),
        ("apk", "apk"),
    ]:
        if shutil.which(cmd):
            return pm
    return None


def distro() -> str:
    """Detect the OS/distribution with rich detail."""
    system = platform.system()
    if sys.platform == "darwin":
        version = platform.mac_ver()[0]
        arch = platform.machine()
        base = f"macOS {version}".strip()
        if arch == "arm64":
            base += " (Apple Silicon)"
        else:
            base += " (Intel)"
        return base
    if sys.platform == "win32":
        version = platform.version()
        edition = platform.win32_edition() if hasattr(platform, "win32_edition") else ""
        arch = platform.machine()
        base = f"Windows {version}".strip()
        if edition:
            base += f" {edition}"
        if arch:
            base += f" ({arch})"
        return base
    # Linux — parse /etc/os-release for detailed info
    try:
        with open("/etc/os-release", encoding="utf-8") as fh:
            content = fh.read()
            pretty = re.search(r'^PRETTY_NAME="?(.*?)"?\s*$', content, re.M)
            if pretty:
                return pretty.group(1)
            name = re.search(r'^NAME="?(.*?)"?\s*$', content, re.M)
            version_id = re.search(r'^VERSION_ID="?(.*?)"?\s*$', content, re.M)
            if name:
                return f"{name.group(1)} {version_id.group(1) if version_id else ''}".strip()
    except OSError:
        pass
    # Fallback: try lsb_release
    lsb = shutil.which("lsb_release")
    if lsb:
        try:
            out = run([lsb, "-ds"])
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip().strip('"')
        except Exception:
            pass
    return platform.platform()


def find_python() -> str | None:
    """Newest usable python3.12+ on PATH (or the `py` launcher on Windows)."""
    candidates = ["python3.12", "python3.13", "python3.11", "python3", "python"]
    if sys.platform == "win32":
        candidates = ["py", "python", "python3"]
    best: tuple[tuple[int, int], str] | None = None
    for cmd in candidates:
        exe = shutil.which(cmd)
        if not exe:
            continue
        args = [exe, "-3.12"] if cmd == "py" else [exe, "--version"]
        try:
            out = run(args)
            text = (out.stdout or out.stderr).strip()
            match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
            if match and (int(match.group(1)), int(match.group(2))) >= MIN_PYTHON:
                ver = (int(match.group(1)), int(match.group(2)))
                if best is None or ver > best[0]:
                    best = (ver, exe)
        except Exception:
            continue
    return best[1] if best else None


def tcp_open(host: str, port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1.5)
    try:
        return sock.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def detect() -> dict[str, object]:
    info: dict[str, object] = {
        "os": platform.system(),
        "platform_group": get_platform_group(),
        "distro": distro(),
        "arch": platform.machine(),
        "python": find_python(),
        "uv": shutil.which("uv"),
        "git": shutil.which("git"),
        "docker": shutil.which("docker"),
        "package_manager": detect_package_manager(),
        "redis_up": tcp_open("127.0.0.1", 6379),
        "port_8000_busy": tcp_open("127.0.0.1", 8000),
    }
    compose = shutil.which("docker-compose")
    plugin = False
    if info["docker"]:
        try:
            plugin = run(["docker", "compose", "version"]).returncode == 0
        except Exception:
            plugin = False
    info["compose"] = bool(compose or plugin)
    return info


# ---------------------------------------------------------------------------
# Installers (always confirmed unless --yes)
# ---------------------------------------------------------------------------


def linux_pkg_manager() -> str | None:
    for mgr in ("apt-get", "dnf", "pacman", "apk", "zypper"):
        if shutil.which(mgr):
            return mgr
    return None


def install_python(assume_yes: bool) -> str | None:
    system = platform.system()
    platform_group = get_package_group()
    cmds: list[str] = []
    fallback_hints: list[str] = []

    if system == "Linux":
        mgr = linux_pkg_manager()
        if mgr == "apt-get":
            cmds = ["sudo apt-get update && sudo apt-get install -y python3.12 python3.12-venv git curl"]
            fallback_hints = [
                "Ubuntu/Debian: sudo apt-get install -y python3.12 python3.12-venv git curl",
                "Or use deadsnakes PPA: sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt-get install -y python3.12",
            ]
        elif mgr == "dnf":
            cmds = ["sudo dnf install -y python3.12 git curl"]
            fallback_hints = [
                "Fedora: sudo dnf install -y python3.12 git curl",
                "If python3.12 is not available: sudo dnf install -y python3 git curl",
            ]
        elif mgr == "pacman":
            cmds = ["sudo pacman -Sy --noconfirm python git curl"]
            fallback_hints = [
                "Arch Linux: sudo pacman -Sy --noconfirm python git curl",
                "Python 3.12+ is included in the default 'python' package on Arch.",
            ]
        elif mgr == "apk":
            cmds = ["sudo apk add python3 git curl"]
            fallback_hints = [
                "Alpine: sudo apk add python3 git curl py3-pip",
            ]
        elif mgr == "zypper":
            cmds = ["sudo zypper install -y python312 git curl"]
            fallback_hints = [
                "openSUSE: sudo zypper install -y python312 git curl",
            ]
        else:
            fallback_hints = [
                "Install Python 3.12+ using your distribution's package manager.",
                "Or build from source: https://www.python.org/downloads/",
            ]
    elif system == "Darwin":
        if shutil.which("brew"):
            cmds = ["brew install python@3.12 git"]
            fallback_hints = [
                "macOS with Homebrew: brew install python@3.12 git",
                "Or download from https://www.python.org/downloads/macos/",
            ]
        elif shutil.which("port"):
            cmds = ["sudo port install python312 git"]
            fallback_hints = [
                "macOS with MacPorts: sudo port install python312 git",
                "Or install Homebrew: https://brew.sh",
            ]
        else:
            fallback_hints = [
                "Install Homebrew first: /bin/bash -c \"$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"",
                "Then: brew install python@3.12 git",
                "Or download Python from https://www.python.org/downloads/macos/",
            ]
    elif system == "Windows":
        if shutil.which("winget"):
            cmds = ["winget install -e --id Python.Python.3.12 Git.Git"]
            fallback_hints = [
                "Windows with winget: winget install -e --id Python.Python.3.12 Git.Git",
                "Or download from https://www.python.org/downloads/windows/",
                "Or use Chocolatey: choco install python git",
            ]
        elif shutil.which("choco"):
            cmds = ["choco install python git"]
            fallback_hints = [
                "Windows with Chocolatey: choco install python git",
                "Or download from https://www.python.org/downloads/windows/",
            ]
        elif shutil.which("scoop"):
            cmds = ["scoop install python git"]
            fallback_hints = [
                "Windows with Scoop: scoop install python git",
                "Or download from https://www.python.org/downloads/windows/",
            ]
        else:
            fallback_hints = [
                "Download Python from https://www.python.org/downloads/windows/",
                "Or install winget (App Installer) from the Microsoft Store.",
                "Or install Chocolatey: https://chocolatey.org/install",
            ]
    else:
        fallback_hints = [
            f"Unsupported platform '{system}' — install Python 3.12+ manually.",
            "Download from https://www.python.org/downloads/",
        ]

    if not cmds:
        warn("No known package manager detected — install Python 3.12+ manually:")
        for hint in fallback_hints:
            warn(f"  {hint}")
        return None

    log(f"Missing Python 3.12+. Would run: {cmds[0]}")
    if not ask("Install it now?", assume_yes):
        # Still print hints when user declines
        log("Manual install hints:")
        for hint in fallback_hints:
            log(f"  {hint}")
        return None
    try:
        rc = os.system(cmds[0])
    except Exception as e:
        warn(f"Automatic install raised: {e}")
        rc = 1
    if rc != 0:
        warn("Automatic install failed — install Python 3.12+ manually:")
        for hint in fallback_hints:
            warn(f"  {hint}")
        return None
    return find_python()


def get_package_group() -> str:
    """Return normalized package group string for install hints."""
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    return "linux"


def install_uv(python_exe: str, assume_yes: bool) -> str | None:
    if shutil.which("uv"):
        return shutil.which("uv")
    if platform.system() == "Windows":
        cmd = 'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
    else:
        cmd = "curl -LsSf https://astral.sh/uv/install.sh | sh"
    log(f"Missing uv. Would run: {cmd}")
    if not ask("Install uv now?", assume_yes):
        return None
    if os.system(cmd) != 0:
        warn("uv install failed — see https://docs.astral.sh/uv/#installation")
        return None
    # Re-scan PATH (installer drops it in ~/.local/bin or ~/.cargo/bin).
    extra = [os.path.expanduser("~/.local/bin"), os.path.expanduser("~/.cargo/bin")]
    os.environ["PATH"] = os.pathsep.join(extra + [os.environ.get("PATH", "")])
    found = shutil.which("uv")
    if found is None:
        warn("uv installed but not on PATH — restart your shell, then re-run.")
    return found


# ---------------------------------------------------------------------------
# Repo + project steps
# ---------------------------------------------------------------------------


def is_checkout(path: str) -> bool:
    return os.path.isfile(os.path.join(path, "backend", "pyproject.toml")) and os.path.isfile(
        os.path.join(path, "Makefile")
    )


def ensure_repo(args: argparse.Namespace) -> str:
    if is_checkout(os.getcwd()):
        log(f"Using repo at {os.getcwd()}")
        return os.getcwd()
    target = args.dir or os.path.join(os.getcwd(), "bob-agent")
    if is_checkout(target):
        log(f"Using repo at {target}")
        return target
    url = args.repo
    log(f"Cloning {url} → {target}")
    if not shutil.which("git"):
        sys.exit("[bootstrap] git is required to clone. Install git and re-run.")
    rc = run(["git", "clone", url, target])
    if rc.returncode != 0 or not is_checkout(target):
        sys.exit(f"[bootstrap] clone failed:\n{rc.stderr}")
    return target


def step_sync(repo: str) -> None:
    log("Installing backend dependencies (uv sync)…")
    rc = run(["uv", "sync"], cwd=os.path.join(repo, "backend"))
    if rc.returncode != 0:
        sys.exit(f"[bootstrap] `uv sync` failed:\n{rc.stdout}\n{rc.stderr}")
    # Best-effort editable install so `agentctl` lands on PATH.
    run(["uv", "pip", "install", "--python", ".venv/bin/python", "-e", "."],
        cwd=os.path.join(repo, "backend"))
    log("Dependencies installed.")


def step_migrate(repo: str) -> None:
    log("Running database migrations…")
    rc = run(["uv", "run", "alembic", "upgrade", "head"], cwd=os.path.join(repo, "backend"))
    if rc.returncode != 0:
        warn(f"migrations failed (continuing):\n{rc.stderr[-2000:]}")
    else:
        log("Migrations done.")


def step_redis(repo: str, assume_yes: bool, no_docker: bool) -> None:
    if tcp_open("127.0.0.1", 6379):
        log("Redis already reachable on :6379.")
        return
    if no_docker:
        warn("Redis not reachable and --no-docker given — the RQ worker will fail; start Redis manually.")
        _print_redis_hints()
        return

    # Check for Docker (preferred method)
    has_docker = shutil.which("docker") is not None
    has_compose = shutil.which("docker-compose") is not None or _has_compose_plugin()

    if has_docker and has_compose:
        log("Starting Redis via `docker compose up -d`…")
        rc = run(["docker", "compose", "up", "-d"], cwd=repo)
        if rc.returncode != 0:
            rc = run(["docker-compose", "up", "-d"], cwd=repo)
        if rc.returncode != 0:
            warn(f"`docker compose up` failed:\n{rc.stderr[-2000:]}")
            _print_redis_hints()
        else:
            log("Redis container started.")
        return

    # No Docker — offer platform-specific alternatives
    if not has_docker:
        warn("Docker not found — Redis requires Docker or manual installation.")
    elif not has_compose:
        warn("Docker Compose not found — install it or start Redis manually.")

    _print_redis_hints()


def _print_redis_hints() -> None:
    """Print platform-specific Redis installation hints."""
    system = platform.system()
    log("Redis installation options:")

    if system == "Linux":
        mgr = linux_pkg_manager()
        if mgr == "apt-get":
            log("  Ubuntu/Debian: sudo apt-get install -y redis-server")
            log("                  sudo systemctl enable --now redis-server")
        elif mgr == "dnf":
            log("  Fedora: sudo dnf install -y redis")
            log("          sudo systemctl enable --now redis")
        elif mgr == "pacman":
            log("  Arch: sudo pacman -S redis")
            log("        sudo systemctl enable --now redis")
        elif mgr == "apk":
            log("  Alpine: sudo apk add redis")
            log("          sudo rc-update add redis default && sudo rc-service redis start")
        else:
            log("  Install Redis via your distribution's package manager.")
        log("  Or use Docker: https://docs.docker.com/get-docker/")
    elif system == "Darwin":
        if shutil.which("brew"):
            log("  macOS: brew install redis")
            log("         brew services start redis")
        else:
            log("  Install Homebrew: https://brew.sh")
            log("  Then: brew install redis && brew services start redis")
        log("  Or use Docker: https://docs.docker.com/get-docker/")
    elif system == "Windows":
        log("  Windows options:")
        log("    1. Use Docker (recommended): https://docs.docker.com/get-docker/")
        log("    2. Use WSL2: wsl --install, then install Redis in WSL")
        log("    3. Download Redis for Windows: https://github.com/microsoftarchive/redis/releases")
        log("    4. Use Memurai (Redis-compatible): https://www.memurai.com/")
    else:
        log("  Install Redis: https://redis.io/download")
        log("  Or use Docker: https://docs.docker.com/get-docker/")


def _has_compose_plugin() -> bool:
    try:
        return run(["docker", "compose", "version"]).returncode == 0
    except Exception:
        return False


def step_setup(repo: str, assume_yes: bool, skip_setup: bool) -> None:
    backend = os.path.join(repo, "backend")
    log("System check (agentctl doctor)…")
    doc = run(["uv", "run", "agentctl", "doctor"], cwd=backend)
    print(doc.stdout or doc.stderr)
    if skip_setup:
        log("Skipping interactive setup (--skip-setup). Run `make setup` later.")
        return
    cmd = ["uv", "run", "agentctl", "setup"]
    if assume_yes:
        cmd.append("--yes")
    log(f"Launching setup wizard: {' '.join(cmd)}")
    rc = subprocess.run(cmd, cwd=backend)
    if rc.returncode != 0:
        warn("setup wizard exited non-zero — re-run with `make setup`.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bob Agent one-shot bootstrapper.")
    parser.add_argument("--repo", default=DEFAULT_REPO_URL, help="Git URL to clone")
    parser.add_argument("--dir", default=None, help="Checkout directory")
    parser.add_argument("--yes", action="store_true", help="Auto-install + non-interactive setup")
    parser.add_argument("--skip-setup", action="store_true", help="Skip the setup wizard")
    parser.add_argument("--no-docker", action="store_true", help="Do not start Redis via Docker")
    args = parser.parse_args(argv)

    info = detect()
    log(f"{info['os']} · {info['distro']} · {info['arch']}")

    # Print platform-specific notes
    _print_platform_notes(info)

    python_exe = info["python"] if isinstance(info["python"], str) else None
    if not python_exe:
        warn("No Python 3.12+ found.")
        python_exe = install_python(args.yes)
        if not python_exe:
            return 1
    log(f"Python: {python_exe}")

    uv_exe = info["uv"] if isinstance(info["uv"], str) else None
    if not uv_exe:
        uv_exe = install_uv(python_exe, args.yes)
        if not uv_exe:
            return 1
    log(f"uv: {uv_exe}")

    if not info["git"]:
        warn("git not found — needed for cloning / git-based skills. Install it when asked.")
    if info["port_8000_busy"]:
        warn("Port 8000 is already in use — `make start` may fail to bind.")

    repo = ensure_repo(args)
    step_sync(repo)
    step_migrate(repo)
    step_redis(repo, args.yes, args.no_docker)
    step_setup(repo, args.yes, args.skip_setup)

    print()
    log("Done. Next:  `make start`  (API :8000 + RQ worker)")
    return 0


def _print_platform_notes(info: dict) -> None:
    """Print platform-specific notes for the detected system."""
    platform_group = info.get("package_manager")
    system = info.get("os", "")

    if system == "Linux":
        log("Platform notes:")
        log("  - Ensure your user is in the 'docker' group to run Docker without sudo:")
        log("    sudo usermod -aG docker $USER && newgrp docker")
        log("  - If ports are blocked, check firewall: sudo ufw allow 8000/tcp && sudo ufw allow 3000/tcp")
    elif system == "Darwin":
        log("Platform notes:")
        log("  - On Apple Silicon Macs, ensure Rosetta 2 is installed for x86 containers:")
        log("    softwareupdate --install-rosetta --agree-to-license")
        log("  - Docker Desktop for Mac is required for container support.")
    elif system == "Windows":
        log("Platform notes:")
        log("  - Enable WSL2 for best compatibility: wsl --install")
        log("  - Docker Desktop with WSL2 backend is recommended.")
        log("  - Run PowerShell as Administrator for system-wide installs.")


if __name__ == "__main__":
    raise SystemExit(main())
