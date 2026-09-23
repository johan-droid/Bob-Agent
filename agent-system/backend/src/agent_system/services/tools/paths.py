"""Workspace path jail + output scrubbing shared by every capability.

All filesystem reachability decisions live here so no capability can invent a
looser rule. The jail resolves symlinks *before* the containment check, so a
symlink pointing outside the allowed roots is rejected, not followed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_system.services.tool_errors import ToolError


def allowed_roots(settings: Any) -> list[Path]:
    """Every root a capability may touch for this deployment, resolved."""
    # Never include bare cwd: it would make the whole repo writable.
    roots = [Path.cwd() / "workspaces", Path.cwd() / "outputs"]
    extra = str(getattr(settings, "tools_fs_roots", "") or "")
    for part in extra.split(","):
        part = part.strip()
        if part:
            roots.append(Path(part).expanduser())
    seen: list[Path] = []
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved not in seen:
            seen.append(resolved)
    return seen


def is_within(path: Path, root: Path) -> bool:
    try:
        return path == root or path.is_relative_to(root)
    except (OSError, ValueError):
        return False


def jailed(path_str: str, settings: Any, *, must_exist: bool = False) -> Path:
    """Resolve a requested path, enforcing jail roots + secret exclusion."""
    from agent_system.services.secrets import is_secret_path

    if not path_str:
        raise ToolError("a path is required")
    if "\x00" in path_str:
        raise ToolError("refusing a path containing a NUL byte")
    candidate = Path(path_str).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise ToolError(f"unresolvable path: {path_str}") from exc
    if is_secret_path(resolved.name) or is_secret_path(str(resolved)):
        raise ToolError(f"refusing secret path: {path_str}")
    for root in allowed_roots(settings):
        if is_within(resolved, root):
            if must_exist and not resolved.exists():
                raise ToolError(f"no such file or directory: {path_str}")
            return resolved
    raise ToolError(f"path outside allowed roots: {path_str}")


def relative_label(path: Path) -> str:
    """Workspace-relative label for display; falls back to the absolute path."""
    for root in (Path.cwd(),):
        try:
            return str(path.relative_to(root))
        except ValueError:
            continue
    return str(path)


def scrub(text: str) -> str:
    """Redact secrets from capability output (best effort, never raises)."""
    try:
        from agent_system.services.memory import scrub_text

        return scrub_text(text)
    except Exception:
        return text


def max_file_bytes(settings: Any) -> int:
    return int(getattr(settings, "max_file_size_mb", 10) or 10) * 1024 * 1024


__all__ = [
    "allowed_roots",
    "is_within",
    "jailed",
    "max_file_bytes",
    "relative_label",
    "scrub",
]
