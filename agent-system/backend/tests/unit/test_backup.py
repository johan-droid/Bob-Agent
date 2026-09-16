"""P1 — Backup cadence for SQLite + vault + recordings.

Acceptance: a run produces a restorable SQLite file (opened + queried);
secret-bearing paths are excluded from the archive; retention prunes;
restore requires explicit confirmation and refuses a live server.
"""

from __future__ import annotations

import socket
import sqlite3
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_system.services.backup import BackupError, BackupService


def _settings(tmp_path: Path, **overrides: Any) -> SimpleNamespace:
    db = tmp_path / "agent_system.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO probe (v) VALUES ('hello')")
    vault = tmp_path / "vault"
    (vault / "memory").mkdir(parents=True)
    (vault / "memory" / "note.md").write_text("# note\nhello\n", encoding="utf-8")
    (vault / ".env").write_text("SECRET=topsecret\n", encoding="utf-8")
    recs = tmp_path / "recordings"
    recs.mkdir()
    (recs / "run.jsonl").write_text('{"a": 1}\n', encoding="utf-8")
    (recs / "credentials.json").write_text('{"token": "x"}\n', encoding="utf-8")
    base = {
        "database_url": f"sqlite:///{db}",
        "vault_path": vault,
        "recordings_dir": recs,
        "backups_dir": tmp_path / "backups",
        "backup_retention_count": 7,
        "api_port": 18099,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestBackupRun:
    def test_produces_restorable_sqlite(self, tmp_path: Path) -> None:
        service = BackupService(_settings(tmp_path))
        record = service.run("run1")
        db_file = record.path / "agent_system.sqlite3"
        assert db_file.exists()
        with sqlite3.connect(db_file) as conn:
            rows = conn.execute("SELECT v FROM probe").fetchall()
        assert rows == [("hello",)]
        assert (record.path / "manifest.json").exists()

    def test_secret_paths_excluded(self, tmp_path: Path) -> None:
        service = BackupService(_settings(tmp_path))
        record = service.run("run1")
        archive = record.path / "vault_recordings.tar.gz"
        assert archive.exists()
        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()
        assert any("note.md" in n for n in names)
        assert any("run.jsonl" in n for n in names)
        assert not any(".env" in n for n in names), names
        assert not any("credentials.json" in n for n in names), names
        assert record.skipped_secrets, "skipped secrets should be reported"

    def test_retention_pruning(self, tmp_path: Path) -> None:
        service = BackupService(_settings(tmp_path, backup_retention_count=2))
        service.run("a")
        service.run("b")
        service.run("c")
        ids = [r.backup_id for r in service.list_backups()]
        assert ids == ["b", "c"]

    def test_failed_run_emits_event_when_bus_present(self, tmp_path: Path) -> None:
        from agent_system.infra.event_bus import EventBus

        settings = _settings(tmp_path)
        settings.database_url = "sqlite:///:memory:"
        service = BackupService(settings, factory=None, event_bus=EventBus())
        with pytest.raises(BackupError):
            service.run("nope")


class TestRestore:
    def test_requires_explicit_confirm(self, tmp_path: Path) -> None:
        service = BackupService(_settings(tmp_path))
        service.run("run1")
        with pytest.raises(BackupError, match="confirm"):
            service.restore("run1", confirm=False)

    def test_unknown_id_rejected(self, tmp_path: Path) -> None:
        service = BackupService(_settings(tmp_path))
        with pytest.raises(BackupError, match="not found"):
            service.restore("missing", confirm=True)

    def test_refuses_live_server_without_force(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        service = BackupService(settings)
        service.run("run1")
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind(("127.0.0.1", settings.api_port))
            server.listen(1)
            with pytest.raises(BackupError, match="live server"):
                service.restore("run1", confirm=True)
            dest = service.restore("run1", confirm=True, force_live=True)
            assert dest.exists()
        finally:
            server.close()

    def test_restore_round_trip(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        service = BackupService(settings)
        service.run("run1")
        db_path = Path(str(settings.database_url)[len("sqlite:///") :])
        with sqlite3.connect(db_path) as conn:
            conn.execute("DELETE FROM probe")
        service.restore("run1", confirm=True)
        with sqlite3.connect(db_path) as conn:
            assert conn.execute("SELECT v FROM probe").fetchall() == [("hello",)]
