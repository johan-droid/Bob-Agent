"""Security + unit tests — Memory + Vault (v3.1 Phase 8 acceptance).

Acceptance under test: persistence · retrieval quality smoke · secrets never
land in the vault or in vectors.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_system.services.memory import (
    HashEmbedding,
    MemoryError,
    MemoryLayer,
    MemoryRecord,
    MemoryStore,
    NoteMeta,
    ObsidianVaultWriter,
    contains_secret,
    scrub_text,
)


@pytest.fixture()
def vault(tmp_path: Path) -> ObsidianVaultWriter:
    return ObsidianVaultWriter(tmp_path / "vault")


@pytest.fixture()
def store(vault: ObsidianVaultWriter) -> MemoryStore:
    return MemoryStore(vault)


class TestSecretFiltering:
    def test_scrub_text_redacts_known_shapes(self) -> None:
        text = "key sk-abcdefghijklmnop1234 and AKIAIOSFODNN7EXAMPLE"
        cleaned = scrub_text(text)
        assert "sk-abcdefghijklmnop1234" not in cleaned
        assert "AKIAIOSFODNN7EXAMPLE" not in cleaned
        assert "[REDACTED]" in cleaned

    def test_contains_secret_detects(self) -> None:
        assert contains_secret("token: ghp_" + "a" * 40)
        assert not contains_secret("harmless text")

    def test_secrets_never_reach_vault(
        self, store: MemoryStore, vault: ObsidianVaultWriter
    ) -> None:
        store.remember(
            "user shared key sk-abcdefghijklmnop1234 for deployment",
            layer=MemoryLayer.USER,
            source="test",
        )
        for note in vault.list_notes():
            raw = note.read_text(encoding="utf-8")
            assert "sk-abcdefghijklmnop1234" not in raw
            assert "[REDACTED]" in raw or "key" in raw  # content survives redacted

    def test_secrets_never_reach_vectors(self, store: MemoryStore) -> None:
        rec = store.remember(
            "credential ghp_" + "a" * 40 + " stored safely",
            layer=MemoryLayer.TASK,
            source="test",
            write_note=False,
        )
        assert "ghp_" + "a" * 40 not in rec.content
        # embedding must derive from scrubbed content, never the raw secret
        scrubbed_vec = HashEmbedding().embed(rec.content)
        assert rec.embedding == scrubbed_vec


class TestVaultNotes:
    def test_note_has_frontmatter_and_attribution(self, vault: ObsidianVaultWriter) -> None:
        path = vault.write_note(
            NoteMeta(
                title="Research: deploy pipeline",
                layer=MemoryLayer.TASK,
                source="task_01J",
                task_id="task_01J",
                session_id="ses_01J",
                tags=["research"],
                links=[[1]][0] and ["Related Note"],
            ),
            body="Findings about the deploy pipeline.",
        )
        assert path.exists()
        parsed = vault.read_note(path)
        fm = parsed["frontmatter"]
        assert fm["layer"] == "TASK"
        assert fm["source"] == "task_01J"
        assert fm["task_id"] == "task_01J"
        assert fm["session_id"] == "ses_01J"
        assert "created" in fm  # timestamp attribution
        assert "[[Related Note]]" in parsed["body"]

    def test_layer_folder_convention(self, vault: ObsidianVaultWriter) -> None:
        path = vault.write_note(
            NoteMeta(title="sys", layer=MemoryLayer.SYSTEM, source="boot"),
            body="x",
        )
        assert "memory/system" in str(path)

    def test_note_body_scrubbed(self, vault: ObsidianVaultWriter) -> None:
        path = vault.write_note(
            NoteMeta(title="leak", layer=MemoryLayer.USER, source="t"),
            body="secret xoxb-1234567890-abcdefghij",
        )
        assert "xoxb-1234567890" not in path.read_text(encoding="utf-8")

    def test_note_size_cap(self, vault: ObsidianVaultWriter) -> None:
        with pytest.raises(MemoryError, match="exceeds"):
            vault.write_note(
                NoteMeta(title="big", layer=MemoryLayer.TASK, source="t"),
                body="x" * (600_000),
            )

    def test_empty_title_rejected(self, vault: ObsidianVaultWriter) -> None:
        with pytest.raises(MemoryError, match="title"):
            vault.write_note(NoteMeta(title="  ", layer=MemoryLayer.TASK, source="t"), body="x")


class TestMemoryStore:
    def test_remember_persists_note(self, store: MemoryStore, vault: ObsidianVaultWriter) -> None:
        store.remember("Prefers dark mode UI", layer=MemoryLayer.USER, source="chat")
        assert len(vault.list_notes()) == 1

    def test_retrieval_ranks_relevant_first(self, store: MemoryStore) -> None:
        store.remember(
            "python testing with pytest fixtures",
            MemoryLayer.TASK,
            "a",
            write_note=False,
        )
        store.remember(
            "docker container networking limits",
            MemoryLayer.TASK,
            "a",
            write_note=False,
        )
        store.remember("pytest parametrize marks", MemoryLayer.TASK, "a", write_note=False)
        hits = store.recall("pytest fixtures testing", top_k=2)
        assert len(hits) == 2
        assert "pytest" in hits[0][0].content

    def test_layer_isolation(self, store: MemoryStore) -> None:
        store.remember("system boot config", MemoryLayer.SYSTEM, "boot", write_note=False)
        store.remember("user likes tea", MemoryLayer.USER, "chat", write_note=False)
        sys_hits = store.recall("boot config", layer=MemoryLayer.SYSTEM)
        assert sys_hits and "system boot" in sys_hits[0][0].content
        user_hits = store.recall("boot config", layer=MemoryLayer.USER)
        assert all("system boot" not in r.content for r, _ in user_hits)

    def test_session_scoping(self, store: MemoryStore) -> None:
        store.remember(
            "alpha session context",
            MemoryLayer.TASK,
            "t",
            session_id="ses_A",
            write_note=False,
        )
        store.remember(
            "beta session context",
            MemoryLayer.TASK,
            "t",
            session_id="ses_B",
            write_note=False,
        )
        hits = store.recall("context", session_id="ses_B")
        assert all(r.session_id == "ses_B" for r, _ in hits)

    def test_forget_and_forget_session(self, store: MemoryStore) -> None:
        r1 = store.remember("one", MemoryLayer.TASK, "t", session_id="s1", write_note=False)
        store.remember("two", MemoryLayer.TASK, "t", session_id="s1", write_note=False)
        store.remember("three", MemoryLayer.TASK, "t", session_id="s2", write_note=False)
        assert store.forget(r1.memory_id) is True
        assert store.forget("mem_missing") is False
        assert store.forget_session("s1") == 1
        assert len(store.all()) == 1

    def test_workspace_layer_record(self, store: MemoryStore) -> None:
        rec: MemoryRecord = store.remember(
            "repo layout", MemoryLayer.WORKSPACE, "ws", workspace_id="ws_9", write_note=False
        )
        assert rec.workspace_id == "ws_9"

    def test_hash_embedding_deterministic(self) -> None:
        e1 = HashEmbedding().embed("same text")
        e2 = HashEmbedding().embed("same text")
        assert e1 == e2
        assert abs(sum(v * v for v in e1) - 1.0) < 1e-9  # unit norm
