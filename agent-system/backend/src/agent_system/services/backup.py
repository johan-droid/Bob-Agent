"""Backup service — SQLite + vault + recordings snapshots.

Each ``run()`` produces one directory ``<backups_dir>/<backup_id>/`` with:

- ``agent_system.sqlite3`` — copied via the ``sqlite3`` online backup API
  (safe under WAL; never a raw file copy of a live database),
- ``vault_recordings.tar.gz`` — tar snapshot of the vault + recordings dirs,
  excluding secret-bearing paths via the **same** ``secrets.is_secret_path``
  predicate ``TemplateManager.snapshot()`` uses (reused, not duplicated),
- ``manifest.json`` — id, timestamp, sizes, skipped secret paths.

Retention keeps the newest ``backup_retention_count`` runs and prunes older
ones. Every run emits ``backup.completed`` / ``backup.failed`` on the event
bus when a factory + bus are provided (same emit pattern as react_agent).

Restore is deliberately strict: it requires explicit ``confirm=True`` (the
CLI ``--confirm`` flag) and refuses to run while a live server answers on
``api_port`` unless ``force_live=True`` (the CLI ``--force-live`` flag).
"""

from __future__ import annotations

import io
import json as _json
import socket
import sqlite3
import tarfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_system.services.secrets import is_secret_path

MANIFEST_NAME = "manifest.json"
DB_NAME = "agent_system.sqlite3"
ARCHIVE_NAME = "vault_recordings.tar.gz"


class BackupError(RuntimeError):
    """A backup or restore run failed (surfaced, never silent)."""


@dataclass
class BackupRecord:
    backup_id: str
    path: Path
    created_at: str
    db_bytes: int
    archive_bytes: int
    skipped_secrets: list[str]


class BackupService:
    def __init__(
        self,
        settings: Any,
        factory: Any | None = None,
        event_bus: Any | None = None,
    ) -> None:
        self._settings = settings
        self._factory = factory
        self._bus = event_bus

    # -- paths -----------------------------------------------------------

    @property
    def backups_dir(self) -> Path:
        return Path(getattr(self._settings, "backups_dir", "backups"))

    @property
    def retention(self) -> int:
        return int(getattr(self._settings, "backup_retention_count", 7) or 7)

    def _db_path(self) -> Path:
        url = str(getattr(self._settings, "database_url", "") or "")
        if url == "sqlite:///:memory:":
            raise BackupError("refusing to back up an in-memory database")
        if not url.startswith("sqlite:///"):
            raise BackupError(
                f"only sqlite databases are backed up (got {url.split(':')[0] or 'unknown'})"
            )
        return Path(url[len("sqlite:///") :]).expanduser()

    def _data_dirs(self) -> list[tuple[str, Path]]:
        settings = self._settings
        pairs = [
            ("vault", Path(getattr(settings, "vault_path", "vault"))),
            ("recordings", Path(getattr(settings, "recordings_dir", "recordings"))),
        ]
        return [(name, path) for name, path in pairs if path.exists()]

    # -- run -------------------------------------------------------------

    def run(self, backup_id: str | None = None) -> BackupRecord:
        stamp = backup_id or datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = self.backups_dir / stamp
        try:
            dest.mkdir(parents=True, exist_ok=False)
            db_bytes = self._backup_db(dest / DB_NAME)
            archive_bytes, skipped = self._backup_archive(dest / ARCHIVE_NAME)
            record = BackupRecord(
                backup_id=stamp,
                path=dest,
                created_at=datetime.now().isoformat(),
                db_bytes=db_bytes,
                archive_bytes=archive_bytes,
                skipped_secrets=skipped,
            )
            (dest / MANIFEST_NAME).write_text(
                _json.dumps(
                    {
                        "backup_id": record.backup_id,
                        "created_at": record.created_at,
                        "db_bytes": record.db_bytes,
                        "archive_bytes": record.archive_bytes,
                        "skipped_secrets": record.skipped_secrets,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            pruned = self.prune()
            self._emit(
                "backup.completed",
                {"backup_id": stamp, "pruned": pruned},
            )
            return record
        except Exception as exc:
            self._emit("backup.failed", {"backup_id": stamp, "error": str(exc)})
            raise BackupError(f"backup {stamp} failed: {exc}") from exc

    def _backup_db(self, dest: Path) -> int:
        import logging as _logging
        import os as _os

        src = self._db_path()
        if not src.exists():
            raise BackupError(f"database file not found: {src}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        _logging.getLogger(__name__).warning(
            "backup contains user_credentials/MemoryNote/TelegramUpdate rows; "
            "encrypt at rest and restrict access (chmod 0600 applied)"
        )
        with sqlite3.connect(src) as src_conn, sqlite3.connect(dest) as dst_conn:
            src_conn.backup(dst_conn)
        try:
            _os.chmod(dest, 0o600)
            _os.chmod(dest.parent, 0o700)
        except Exception:
            pass
        return dest.stat().st_size

    def _backup_archive(self, dest: Path) -> tuple[int, list[str]]:
        import os as _os2

        skipped: list[str] = []
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for top_name, root in self._data_dirs():
                for file_path in sorted(root.rglob("*")):
                    if not file_path.is_file():
                        continue
                    try:
                        if ".." in str(file_path.relative_to(root)):
                            skipped.append(f"{top_name}/..-traversal")
                            continue
                    except ValueError:
                        skipped.append(f"{top_name}/outside-root")
                        continue
                    rel = file_path.relative_to(root)
                    arcname = f"{top_name}/{rel}"
                    # Same exclusion predicate as TemplateManager.snapshot().
                    if is_secret_path(str(rel)) or is_secret_path(arcname):
                        skipped.append(arcname)
                        continue
                    tar.add(file_path, arcname=arcname)
        dest.write_bytes(buf.getvalue())
        try:
            _os2.chmod(dest, 0o600)
        except Exception:
            pass
        return dest.stat().st_size, skipped

    # -- list / prune ----------------------------------------------------

    def list_backups(self) -> list[BackupRecord]:
        root = self.backups_dir
        if not root.exists():
            return []
        records: list[BackupRecord] = []
        for child in sorted(root.iterdir()):
            manifest = child / MANIFEST_NAME
            if not child.is_dir() or not manifest.exists():
                continue
            try:
                data = _json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            records.append(
                BackupRecord(
                    backup_id=str(data.get("backup_id", child.name)),
                    path=child,
                    created_at=str(data.get("created_at", "")),
                    db_bytes=int(data.get("db_bytes", 0) or 0),
                    archive_bytes=int(data.get("archive_bytes", 0) or 0),
                    skipped_secrets=list(data.get("skipped_secrets", []) or []),
                )
            )
        return records

    def prune(self) -> int:
        """Delete oldest runs beyond retention; return pruned count."""
        records = self.list_backups()
        excess = len(records) - max(self.retention, 0)
        pruned = 0
        for record in records[: max(excess, 0)]:
            import shutil

            shutil.rmtree(record.path, ignore_errors=True)
            pruned += 1
        return pruned

    # -- restore ---------------------------------------------------------

    def restore(self, backup_id: str, confirm: bool = False, force_live: bool = False) -> Path:
        """Restore a backup over the live data paths (strict guards).

        Requires ``confirm=True`` (CLI ``--confirm``) and refuses when a live
        server answers on the effective port unless ``force_live=True``.
        """
        if not confirm:
            raise BackupError("restore requires explicit confirmation (--confirm)")
        if self._live_server_detected() and not force_live:
            raise BackupError(
                "a live server appears to be running "
                f"(port {self._probe_port()} answers); "
                "stop it or pass --force-live"
            )
        src = self.backups_dir / backup_id
        manifest = src / MANIFEST_NAME
        if not manifest.exists():
            raise BackupError(f"backup '{backup_id}' not found in {self.backups_dir}")
        dest_db = self._db_path()
        dest_db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(src / DB_NAME) as src_conn, sqlite3.connect(dest_db) as dst_conn:
            src_conn.backup(dst_conn)
        self._restore_archive(src / ARCHIVE_NAME)
        self._emit("backup.completed", {"backup_id": backup_id, "restored": True})
        return dest_db

    def _restore_archive(self, archive: Path) -> list[str]:
        restored: list[str] = []
        if not archive.exists():
            return restored
        roots = {name: path for name, path in self._data_dirs()}
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                if member.name.startswith("/") or ".." in Path(member.name).parts:
                    continue
                if is_secret_path(member.name):
                    continue  # defense in depth: never restore secrets
                top, _, rel = member.name.partition("/")
                root = roots.get(top)
                if root is None or not rel:
                    continue
                target = root / rel
                if not str(target.resolve()).startswith(str(root.resolve()) + "/"):
                    continue
                try:
                    tar.extract(member, root, filter="fully_trusted")
                except (TypeError, AttributeError):
                    tar.extract(member, root)
                restored.append(member.name)
        return restored

    def _probe_port(self) -> int:
        """Port for the live-server guard ($PORT-aware, legacy-tolerant).

        Prefers ``effective_port`` (real Settings, $PORT-aware), falls back
        to ``api_port`` (legacy/SimpleNamespace test settings), then 8000.
        """
        port = getattr(self._settings, "effective_port", None)
        if port is None:
            port = getattr(self._settings, "api_port", 8000)
        return int(port or 8000)

    def _live_server_detected(self) -> bool:
        port = self._probe_port()
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return True
        except OSError:
            return False

    # -- events ----------------------------------------------------------

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._factory is None or self._bus is None:
            return
        try:
            from agent_system.domain.events import Event
            from agent_system.infra.db import session_scope

            with session_scope(self._factory) as db:
                self._bus.emit(Event(type=event_type, actor="backup", payload=payload), db)
        except Exception:
            pass  # event emission must never break a backup


__all__ = ["BackupError", "BackupRecord", "BackupService"]
