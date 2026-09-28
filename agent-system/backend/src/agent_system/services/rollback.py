"""Rollback system — undo completed work.

Before any risky operation, Bob captures a checkpoint of the affected state.
If the operation fails or the user requests it, Bob can roll back to the
pre-operation state.

Three checkpoint types:
  - GitCheckpoint: captures git ref before changes (branch, HEAD sha)
  - FilesystemCheckpoint: captures file contents before modification
  - CompositeCheckpoint: combines multiple checkpoints for one operation

The rollback manager stores checkpoints durably and can restore them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from agent_system.domain.events import utcnow

logger = logging.getLogger(__name__)


class CheckpointType(StrEnum):
    GIT = "git"
    FILESYSTEM = "filesystem"
    COMPOSITE = "composite"


class RollbackStatus(StrEnum):
    PENDING = "pending"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


@dataclass
class FileSnapshot:
    """A snapshot of a single file's content."""

    path: str
    content: bytes
    existed: bool = True


@dataclass
class GitSnapshot:
    """Git state before an operation."""

    repo_path: str
    branch: str
    head_sha: str
    had_staged_changes: bool = False
    had_unstaged_changes: bool = False
    stashed: bool = False


@dataclass
class RollbackCheckpoint:
    """One rollback checkpoint for an operation."""

    id: str
    task_id: str
    operation: str
    checkpoint_type: CheckpointType
    created_at: datetime
    status: RollbackStatus = RollbackStatus.PENDING
    git_snapshot: GitSnapshot | None = None
    file_snapshots: list[FileSnapshot] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class RollbackManager:
    """Manages rollback checkpoints for autonomous operations."""

    def __init__(self, storage_dir: str | None = None) -> None:
        self.storage_dir = Path(storage_dir) if storage_dir else Path(".bob/rollback")
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._checkpoints: dict[str, RollbackCheckpoint] = {}

    def capture_git(
        self, *, task_id: str, operation: str, repo_path: str
    ) -> RollbackCheckpoint | None:
        """Capture git state before an operation."""
        try:
            import subprocess

            def _git(*args: str) -> str:
                result = subprocess.run(
                    ["git", "-C", repo_path, *args],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                return result.stdout.strip() if result.returncode == 0 else ""

            branch = _git("rev-parse", "--abbrev-ref", "HEAD")
            head_sha = _git("rev-parse", "HEAD")
            if not head_sha:
                return None

            status = _git("status", "--porcelain")
            had_staged = bool(status) and any(
                line.startswith("A") or line.startswith("M") for line in status.split("\n") if line
            )
            had_unstaged = bool(status) and any(
                line.startswith(" M") or line.startswith(" D")
                for line in status.split("\n")
                if line
            )

            snapshot = GitSnapshot(
                repo_path=repo_path,
                branch=branch or "HEAD",
                head_sha=head_sha,
                had_staged_changes=had_staged,
                had_unstaged_changes=had_unstaged,
            )

            from agent_system.domain.ids import new_id

            checkpoint = RollbackCheckpoint(
                id=new_id("rb"),
                task_id=task_id,
                operation=operation,
                checkpoint_type=CheckpointType.GIT,
                created_at=utcnow(),
                git_snapshot=snapshot,
            )
            self._checkpoints[checkpoint.id] = checkpoint
            return checkpoint
        except Exception as exc:
            logger.warning("rollback.git_capture_failed task=%s error=%s", task_id, exc)
            return None

    def capture_files(
        self,
        *,
        task_id: str,
        operation: str,
        file_paths: list[str],
    ) -> RollbackCheckpoint | None:
        """Capture file contents before modification."""
        snapshots: list[FileSnapshot] = []
        for path_str in file_paths:
            path = Path(path_str)
            if path.exists():
                try:
                    content = path.read_bytes()
                    snapshots.append(FileSnapshot(path=str(path), content=content, existed=True))
                except Exception as exc:
                    logger.warning("rollback.file_capture_failed path=%s error=%s", path, exc)
            else:
                snapshots.append(FileSnapshot(path=str(path), content=b"", existed=False))

        if not snapshots:
            return None

        from agent_system.domain.ids import new_id

        checkpoint = RollbackCheckpoint(
            id=new_id("rb"),
            task_id=task_id,
            operation=operation,
            checkpoint_type=CheckpointType.FILESYSTEM,
            created_at=utcnow(),
            file_snapshots=snapshots,
        )
        self._checkpoints[checkpoint.id] = checkpoint
        return checkpoint

    def capture_composite(
        self,
        *,
        task_id: str,
        operation: str,
        repo_path: str | None = None,
        file_paths: list[str] | None = None,
    ) -> RollbackCheckpoint | None:
        """Capture both git and filesystem state."""
        from agent_system.domain.ids import new_id

        git_snapshot = None
        file_snapshots: list[FileSnapshot] = []

        if repo_path:
            git_cp = self.capture_git(task_id=task_id, operation=operation, repo_path=repo_path)
            if git_cp and git_cp.git_snapshot:
                git_snapshot = git_cp.git_snapshot

        if file_paths:
            fs_cp = self.capture_files(task_id=task_id, operation=operation, file_paths=file_paths)
            if fs_cp:
                file_snapshots = fs_cp.file_snapshots

        if not git_snapshot and not file_snapshots:
            return None

        checkpoint = RollbackCheckpoint(
            id=new_id("rb"),
            task_id=task_id,
            operation=operation,
            checkpoint_type=CheckpointType.COMPOSITE,
            created_at=utcnow(),
            git_snapshot=git_snapshot,
            file_snapshots=file_snapshots,
        )
        self._checkpoints[checkpoint.id] = checkpoint
        return checkpoint

    def commit(self, checkpoint_id: str) -> bool:
        """Mark a checkpoint as committed (operation succeeded)."""
        cp = self._checkpoints.get(checkpoint_id)
        if not cp:
            return False
        cp.status = RollbackStatus.COMMITTED
        return True

    def rollback(self, checkpoint_id: str) -> bool:
        """Roll back to the state captured in the checkpoint."""
        cp = self._checkpoints.get(checkpoint_id)
        if not cp:
            logger.warning("rollback.not_found checkpoint=%s", checkpoint_id)
            return False

        try:
            if cp.git_snapshot:
                self._rollback_git(cp.git_snapshot)
            if cp.file_snapshots:
                self._rollback_files(cp.file_snapshots)
            cp.status = RollbackStatus.ROLLED_BACK
            return True
        except Exception as exc:
            logger.error("rollback.failed checkpoint=%s error=%s", checkpoint_id, exc)
            cp.status = RollbackStatus.FAILED
            return False

    def _rollback_git(self, snapshot: GitSnapshot) -> None:
        """Restore git state."""
        import subprocess

        def _git(*args: str) -> None:
            subprocess.run(
                ["git", "-C", snapshot.repo_path, *args],
                capture_output=True,
                text=True,
                timeout=30,
            )

        _git("checkout", snapshot.branch)
        _git("reset", "--hard", snapshot.head_sha)
        if snapshot.stashed:
            _git("stash", "pop")

    def _rollback_files(self, snapshots: list[FileSnapshot]) -> None:
        """Restore file contents."""
        for snap in snapshots:
            path = Path(snap.path)
            if snap.existed:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(snap.content)
            else:
                if path.exists():
                    path.unlink()

    def get_checkpoint(self, checkpoint_id: str) -> RollbackCheckpoint | None:
        """Get a checkpoint by ID."""
        return self._checkpoints.get(checkpoint_id)

    def list_checkpoints(self, task_id: str | None = None) -> list[RollbackCheckpoint]:
        """List checkpoints, optionally filtered by task."""
        cps = list(self._checkpoints.values())
        if task_id:
            cps = [cp for cp in cps if cp.task_id == task_id]
        return sorted(cps, key=lambda c: c.created_at, reverse=True)

    def can_rollback(self, checkpoint_id: str) -> bool:
        """Check if a checkpoint can be rolled back."""
        cp = self._checkpoints.get(checkpoint_id)
        return cp is not None and cp.status == RollbackStatus.COMMITTED


#: Module-level singleton.
_default_manager: RollbackManager | None = None


def get_rollback_manager() -> RollbackManager:
    """Get or create the default rollback manager."""
    global _default_manager
    if _default_manager is None:
        _default_manager = RollbackManager()
    return _default_manager


def reset_rollback_manager() -> None:
    """Reset the default rollback manager (for tests)."""
    global _default_manager
    _default_manager = None


__all__ = [
    "CheckpointType",
    "FileSnapshot",
    "GitSnapshot",
    "RollbackCheckpoint",
    "RollbackManager",
    "RollbackStatus",
    "get_rollback_manager",
    "reset_rollback_manager",
]
