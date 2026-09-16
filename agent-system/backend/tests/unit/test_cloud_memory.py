"""Cloud vault — DB-backed memory notes (dyno-safe persistence).

Acceptance: DbNoteStore scrubs body + metadata before insert; recall ranks
by keyword overlap (newest first on ties); hooks branch to the DB only when
CLOUD_VAULT_DB=true AND a factory is passed (local file behavior default);
recall on DB never raises.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_system.infra.db import make_engine, make_session_factory, session_scope
from agent_system.infra.models import Base, MemoryNote
from agent_system.services.memory import DbNoteStore, MemoryLayer, NoteMeta
from agent_system.services.memory_hooks import recall_recent, remember_fact


def _factory(tmp_path: Path) -> Any:
    engine = make_engine(f"sqlite:///{tmp_path / 'vault.db'}")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _db_settings(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "cloud_vault_db": True,
        "memory_auto_remember": True,
        "vault_path": "unused-in-db-mode",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestDbNoteStore:
    def test_write_and_recall(self, tmp_path: Path) -> None:
        factory = _factory(tmp_path)
        store = DbNoteStore(factory)
        note_id = store.write_note(
            NoteMeta(title="deploy notes", layer=MemoryLayer.TASK, source="agent"),
            body="heroku procfile release phase runs migrations",
        )
        assert note_id.startswith("mem_")
        hits = store.recall("heroku migrations", limit=3)
        assert len(hits) == 1
        assert hits[0]["title"] == "deploy notes"
        assert hits[0]["path"] == f"db:{note_id}"
        assert "procfile" in hits[0]["snippet"]

    def test_secrets_scrubbed_before_insert(self, tmp_path: Path) -> None:
        factory = _factory(tmp_path)
        store = DbNoteStore(factory)
        store.write_note(
            NoteMeta(
                title="note sk-abcdefghijklmnop1234567890 title",
                layer=MemoryLayer.TASK,
                source="agent",
                tags=["key sk-abcdefghijklmnop1234567890"],
            ),
            body="token sk-abcdefghijklmnop1234567890 here",
        )
        with session_scope(factory) as db:
            row = db.query(MemoryNote).one()
            blob = f"{row.title} {row.body} {' '.join(row.tags_json)}"
            assert "sk-abcdefghijklmnop1234567890" not in blob
            assert "[REDACTED]" in blob

    def test_recall_ranking_overlap_then_newest(self, tmp_path: Path) -> None:
        factory = _factory(tmp_path)
        store = DbNoteStore(factory)
        store.write_note(
            NoteMeta(title="a", layer=MemoryLayer.TASK, source="agent"),
            body="unrelated words here",
        )
        store.write_note(
            NoteMeta(title="b", layer=MemoryLayer.TASK, source="agent"),
            body="heroku heroku heroku dyno",
        )
        hits = store.recall("heroku dyno", limit=5)
        assert hits[0]["title"] == "b"

    def test_empty_title_rejected(self, tmp_path: Path) -> None:
        factory = _factory(tmp_path)
        try:
            DbNoteStore(factory).write_note(
                NoteMeta(title="  ", layer=MemoryLayer.TASK, source="agent"),
                body="x",
            )
        except Exception:
            return
        raise AssertionError("expected empty title to be rejected")


class TestHooksDbBranch:
    def test_remember_fact_goes_to_db(self, tmp_path: Path) -> None:
        factory = _factory(tmp_path)
        settings = _db_settings()
        note_id = remember_fact(settings, "postgres addon attached", factory=factory)
        with session_scope(factory) as db:
            row = db.query(MemoryNote).filter_by(id=note_id).one()
            assert "postgres addon attached" in row.body
        # No files touched (vault_path points at a nonexistent dir).
        assert not Path("unused-in-db-mode").exists()

    def test_recall_recent_reads_db(self, tmp_path: Path) -> None:
        factory = _factory(tmp_path)
        settings = _db_settings()
        remember_fact(settings, "dyno sleeps after thirty minutes", factory=factory)
        hits = recall_recent(settings, "dyno sleep", limit=3, factory=factory)
        assert len(hits) == 1
        assert "dyno" in hits[0]["snippet"]

    def test_flag_off_keeps_file_vault(self, tmp_path: Path) -> None:
        factory = _factory(tmp_path)
        vault = tmp_path / "vault"
        settings = SimpleNamespace(
            cloud_vault_db=False, memory_auto_remember=True, vault_path=vault
        )
        note_id = remember_fact(settings, "local file note", factory=factory)
        assert note_id.startswith("mem_")
        assert list(vault.rglob("*.md"))
        with session_scope(factory) as db:
            assert db.query(MemoryNote).count() == 0

    def test_no_factory_keeps_file_vault(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        settings = SimpleNamespace(cloud_vault_db=True, memory_auto_remember=True, vault_path=vault)
        remember_fact(settings, "cli-style note without factory")
        assert list(vault.rglob("*.md"))
