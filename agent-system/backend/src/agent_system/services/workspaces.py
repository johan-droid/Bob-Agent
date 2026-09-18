"""Workspace management (v3.1 Phase 5) + templates with secret scanning (§22).

Workspaces are plain directories on disk (Docker isolation attaches in the
sandbox phase; all path operations here are traversal-safe). Template
snapshots scan for and exclude secrets before tar'ing.
"""

from __future__ import annotations

import hashlib
import io
import shutil
import tarfile
from pathlib import Path

from agent_system.services.secrets import is_secret_path

MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB per file (v3.1 §30 max_file_size)


class WorkspaceError(ValueError):
    pass


def _resolve_safe(root: Path, rel_path: str) -> Path:
    """Join and verify the result stays inside root (path traversal guard)."""
    candidate = (root / rel_path).resolve()
    if not str(candidate).startswith(str(root.resolve()) + "/") and candidate != root.resolve():
        raise WorkspaceError(f"path traversal blocked: {rel_path}")
    return candidate


class WorkspaceManager:
    def __init__(self, workspaces_dir: Path) -> None:
        self._root = workspaces_dir
        self._root.mkdir(parents=True, exist_ok=True)

    def create(self, workspace_id: str) -> Path:
        ws = self._root / workspace_id
        ws.mkdir(parents=True, exist_ok=False)
        return ws

    def path(self, workspace_id: str) -> Path:
        ws = (self._root / workspace_id).resolve()
        if not str(ws).startswith(str(self._root.resolve()) + "/"):
            raise WorkspaceError("invalid workspace id")
        return ws

    def write_file(self, workspace_id: str, rel_path: str, content: bytes) -> Path:
        if len(content) > MAX_FILE_BYTES:
            raise WorkspaceError(f"file exceeds {MAX_FILE_BYTES} bytes")
        target = _resolve_safe(self.path(workspace_id), rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def read_file(self, workspace_id: str, rel_path: str) -> bytes:
        return _resolve_safe(self.path(workspace_id), rel_path).read_bytes()

    def tree(self, workspace_id: str) -> list[str]:
        ws = self.path(workspace_id)
        return sorted(
            str(p.relative_to(ws)) for p in ws.rglob("*") if p.is_file() and ".git" not in p.parts
        )

    def fingerprint(self, workspace_id: str) -> str:
        """Stable content hash — used by replay safety checks (v3.1 §23)."""
        sha = hashlib.sha256()
        ws = self.path(workspace_id)
        for rel in self.tree(workspace_id):
            sha.update(rel.encode())
            sha.update(b"\0")
            sha.update((ws / rel).read_bytes())
        return sha.hexdigest()

    def delete(self, workspace_id: str) -> None:
        shutil.rmtree(self.path(workspace_id))


class TemplateManager:
    """Snapshot/clone workspaces with mandatory secret exclusion (v3.1 §22)."""

    def __init__(self, templates_dir: Path) -> None:
        self._root = templates_dir
        self._root.mkdir(parents=True, exist_ok=True)

    def snapshot(self, template_id: str, workspace: Path, name: str) -> Path:
        out = self._root / f"{template_id}.tar.gz"
        buf = io.BytesIO()
        skipped: list[str] = []
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for file_path in sorted(workspace.rglob("*")):
                if not file_path.is_file():
                    continue
                rel = file_path.relative_to(workspace)
                rel_str = str(rel)
                if is_secret_path(rel_str):
                    skipped.append(rel_str)
                    continue
                if ".git" in rel.parts and "HEAD" not in rel.parts:
                    continue  # git internals except HEAD marker
                tar.add(file_path, arcname=rel_str)
        out.write_bytes(buf.getvalue())
        self._last_skipped = skipped  # noqa: SLF001 — exposed via last_skipped
        self._name = name  # noqa: SLF001
        return out

    @property
    def last_skipped(self) -> list[str]:
        return getattr(self, "_last_skipped", [])

    def clone(self, template_id: str, target: Path) -> list[str]:
        src = self._root / f"{template_id}.tar.gz"
        if not src.exists():
            raise WorkspaceError(f"template {template_id} not found")
        restored: list[str] = []
        target.mkdir(parents=True, exist_ok=True)
        with tarfile.open(src, "r:gz") as tar:
            safe_members: list[tarfile.TarInfo] = []
            for member in tar.getmembers():
                # Safe extraction: reject absolute paths and traversal (v3.1 §14).
                if member.name.startswith("/") or ".." in Path(member.name).parts:
                    continue
                if is_secret_path(member.name):
                    continue  # defense in depth: never restore secrets
                dest = (target / member.name).resolve()
                if dest != target.resolve() and target.resolve() not in dest.parents:
                    continue
                safe_members.append(member)
            # The explicit filter protects against traversal and other unsafe
            # metadata even for members that pass the path check.
            try:
                tar.extractall(target, members=safe_members, filter="data")
            except (tarfile.TarError, OSError):
                return restored
            restored = [member.name for member in safe_members]
        return restored
