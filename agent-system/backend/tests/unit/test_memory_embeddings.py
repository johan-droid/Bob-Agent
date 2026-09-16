"""P1 — Memory embedding backends (hash default vs opt-in local).

Acceptance: default settings keep the zero-dependency HashEmbedding path
untouched; ``local`` is opt-in and (when the ``memory`` extra is installed)
produces materially better semantic-recall ranking than hash on a synthetic
paraphrase fixture.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_system.services.memory import (
    HashEmbedding,
    LocalEmbeddingProvider,
    MemoryLayer,
    MemoryStore,
    ObsidianVaultWriter,
    build_memory_store,
    embedding_provider_from_settings,
)

SEMANTIC_DOC = "Canine pets enjoy fetching balls in the park"
LEXICAL_TRAP = "The cat sat on the mat near the flat"
UNRELATED_DOC = "Photosynthesis converts sunlight inside chloroplasts"
QUERY = "dogs love playing fetch outdoors"


@pytest.fixture()
def vault(tmp_path: Path) -> ObsidianVaultWriter:
    return ObsidianVaultWriter(tmp_path / "vault")


def _rank_of(store: MemoryStore, query: str, target: str) -> int:
    results = store.recall(query, top_k=10)
    for rank, (rec, _score) in enumerate(results):
        if rec.content == target:
            return rank
    raise AssertionError("target doc missing from recall results")


class TestDefaultUnchanged:
    def test_factory_defaults_to_hash(self) -> None:
        provider = embedding_provider_from_settings(SimpleNamespace())
        assert isinstance(provider, HashEmbedding)

    def test_factory_explicit_hash(self) -> None:
        provider = embedding_provider_from_settings(
            SimpleNamespace(memory_embedding_provider="hash")
        )
        assert isinstance(provider, HashEmbedding)

    def test_store_default_constructor_still_hash(self, vault: ObsidianVaultWriter) -> None:
        store = MemoryStore(vault)
        assert isinstance(store._embedder, HashEmbedding)

    def test_build_memory_store_defaults_to_hash(self, vault: ObsidianVaultWriter) -> None:
        store = build_memory_store(vault, SimpleNamespace())
        assert isinstance(store._embedder, HashEmbedding)

    def test_unknown_provider_fails_closed(self) -> None:
        from agent_system.services.memory import MemoryError

        with pytest.raises(MemoryError, match="unknown"):
            embedding_provider_from_settings(SimpleNamespace(memory_embedding_provider="openai"))


class TestLocalProvider:
    def test_local_provider_model_name(self) -> None:
        assert LocalEmbeddingProvider.MODEL_NAME == "all-MiniLM-L6-v2"

    def test_local_missing_dep_error_mentions_extra(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulate a bare install: hide sentence_transformers and require the
        explicit MemoryError (fail closed, never a silent hash fallback)."""
        monkeypatch.setitem(sys.modules, "sentence_transformers", None)
        from agent_system.services.memory import MemoryError

        with pytest.raises(MemoryError, match="memory.*extra|pip install"):
            embedding_provider_from_settings(SimpleNamespace(memory_embedding_provider="local"))

    def test_local_beats_hash_on_paraphrase_fixture(
        self, vault: ObsidianVaultWriter, tmp_path: Path
    ) -> None:
        hash_store = MemoryStore(vault, HashEmbedding())
        local_store = MemoryStore(
            ObsidianVaultWriter(tmp_path / "vault2"), LocalEmbeddingProvider()
        )
        for store in (hash_store, local_store):
            for doc in (SEMANTIC_DOC, LEXICAL_TRAP, UNRELATED_DOC):
                store.remember(doc, layer=MemoryLayer.USER, source="test", write_note=False)
        hash_rank = _rank_of(hash_store, QUERY, SEMANTIC_DOC)
        local_rank = _rank_of(local_store, QUERY, SEMANTIC_DOC)
        # The paraphrase shares almost no tokens with the query, so the
        # lexical hash baseline ranks it poorly; real embeddings rank it first.
        assert local_rank == 0
        assert local_rank < hash_rank
