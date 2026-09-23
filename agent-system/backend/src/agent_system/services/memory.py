"""Memory + Vault (v3.1 Phase 8, §28 §Memory).

Two components:

1. ObsidianVaultWriter — writes markdown notes with YAML frontmatter and
   [[wiki-links]] into the configured vault, with attribution metadata
   (source/task/session/agent/timestamp) on every note.
2. MemoryStore — layered memory (SYSTEM/USER/TASK/WORKSPACE) with optional
   embedding vectors. When a real embedding provider is not wired, retrieval
   falls back to a deterministic hashed lexical embedding — never fake
   results, just a documented, weaker retrieval mode.

Secret filtering: nothing reaches the vault or the vector index before
secret-shaped values are scrubbed (secrets never land in vault or vectors).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from agent_system.domain import ids
from agent_system.domain.events import utcnow

MAX_NOTE_BYTES = 512_000


class MemoryError(ValueError):
    pass


class MemoryLayer(StrEnum):
    SYSTEM = "SYSTEM"
    USER = "USER"
    TASK = "TASK"
    WORKSPACE = "WORKSPACE"


# ---------------------------------------------------------------------------
# Secret scrubbing for free text
# ---------------------------------------------------------------------------

_SECRET_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"),
)


def scrub_text(text: str) -> str:
    """Remove secret-shaped values from free text before persistence."""
    cleaned = text
    for pattern in _SECRET_VALUE_PATTERNS:
        cleaned = pattern.sub("[REDACTED]", cleaned)
    return cleaned


def contains_secret(text: str) -> bool:
    return any(p.search(text) for p in _SECRET_VALUE_PATTERNS)


# ---------------------------------------------------------------------------
# Obsidian vault writer
# ---------------------------------------------------------------------------


@dataclass
class NoteMeta:
    title: str
    layer: MemoryLayer
    source: str  # what produced this note: task/session/agent
    task_id: str | None = None
    session_id: str | None = None
    agent_run_id: str | None = None
    owner_user_id: str | None = None
    tags: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)  # wiki-link targets


def _scrub_meta(meta: NoteMeta) -> NoteMeta:
    """Scrub everything that persists — title/source/tags/links.

    Body is scrubbed separately by each writer. A direct writer caller
    could otherwise leak a secret via metadata even when the body is clean.
    """
    return NoteMeta(
        title=scrub_text(meta.title),
        layer=meta.layer,
        source=scrub_text(meta.source),
        task_id=meta.task_id,
        session_id=meta.session_id,
        agent_run_id=meta.agent_run_id,
        owner_user_id=meta.owner_user_id,
        tags=[scrub_text(t) for t in (meta.tags or [])],
        links=[scrub_text(link) for link in (meta.links or [])],
    )


class ObsidianVaultWriter:
    """Writes Obsidian-compatible notes: YAML frontmatter + [[wiki-links]]."""

    def __init__(self, vault_path: Path) -> None:
        self._root = vault_path
        (self._root / "memory").mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def _layer_dir(self, layer: MemoryLayer) -> Path:
        d = self._root / "memory" / layer.value.lower()
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _frontmatter(self, meta: NoteMeta, created: datetime) -> dict[str, Any]:
        fm: dict[str, Any] = {
            "title": meta.title,
            "layer": meta.layer.value,
            "source": meta.source,
            "created": created.isoformat(),
            "tags": meta.tags or [],
        }
        if meta.task_id:
            fm["task_id"] = meta.task_id
        if meta.session_id:
            fm["session_id"] = meta.session_id
        if meta.agent_run_id:
            fm["agent_run_id"] = meta.agent_run_id
        if meta.owner_user_id:
            fm["owner_user_id"] = meta.owner_user_id
        return fm

    @staticmethod
    def _slug(title: str) -> str:
        slug = "".join(c if c.isalnum() or c in "-_ " else "-" for c in title).strip()
        slug = re.sub(r"\s+", "-", slug)[:120]
        return slug or "note"

    def write_note(self, meta: NoteMeta, body: str) -> Path:
        if not meta.title.strip():
            raise MemoryError("note title required")
        safe_meta = _scrub_meta(meta)
        body = scrub_text(body)
        created = utcnow()
        fm = self._frontmatter(safe_meta, created)
        fm_yaml = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
        links = "".join(f" [[{link}]]" for link in (safe_meta.links or []))
        content = f"---\n{fm_yaml}\n---\n\n# {safe_meta.title}\n\n{body}{links}\n"
        if len(content.encode("utf-8")) > MAX_NOTE_BYTES:
            raise MemoryError(f"note exceeds {MAX_NOTE_BYTES} bytes")
        note_path = (
            self._layer_dir(safe_meta.layer)
            / f"{created.strftime('%Y%m%d-%H%M%S')}-{self._slug(safe_meta.title)}.md"
        )
        note_path.write_text(content, encoding="utf-8")
        return note_path

    def read_note(self, note_path: Path) -> dict[str, Any]:
        raw = note_path.read_text(encoding="utf-8")
        if not raw.startswith("---"):
            return {"frontmatter": {}, "body": raw}
        _, fm_text, body = raw.split("---", 2)
        fm = yaml.safe_load(fm_text) or {}
        return {"frontmatter": fm, "body": body.strip()}

    def list_notes(self, layer: MemoryLayer | None = None) -> list[Path]:
        base = self._root / "memory"
        if layer is not None:
            d = base / layer.value.lower()
            return sorted(d.glob("*.md")) if d.exists() else []
        return sorted(base.rglob("*.md"))


# ---------------------------------------------------------------------------
# Memory store with retrieval
# ---------------------------------------------------------------------------


@dataclass
class MemoryRecord:
    memory_id: str
    layer: MemoryLayer
    content: str
    session_id: str | None
    task_id: str | None
    workspace_id: str | None
    created_at: datetime
    embedding: list[float] | None = None


class EmbeddingProvider:
    """Interface; production wires a real embedding provider at composition."""

    def embed(self, text: str) -> list[float]:
        raise NotImplementedError


class HashEmbedding(EmbeddingProvider):
    """Deterministic local embedding (hashed bag-of-words, 64 dims).

    Honest about what it is: a lexical baseline so retrieval works offline.
    Real semantic embeddings plug in via EmbeddingProvider at composition.
    """

    DIM = 64

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.DIM
        for token in re.findall(r"[a-z0-9]+", text.lower()):
            vec[hash(token) % self.DIM] += 1.0
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]


class LocalEmbeddingProvider(EmbeddingProvider):
    """Real semantic embeddings via sentence-transformers (opt-in).

    Uses ``all-MiniLM-L6-v2`` — CPU-friendly, no API key, consistent with the
    offline-first ethos. Requires the ``memory`` extra
    (``pip install agent-system[memory]``); construction raises
    ``MemoryError`` with install instructions when the dependency is missing
    so the failure is explicit, never a silent fallback.
    """

    MODEL_NAME = "all-MiniLM-L6-v2"
    DIM = 384

    def __init__(self, model_name: str = MODEL_NAME) -> None:
        try:
            from sentence_transformers import (
                SentenceTransformer,
            )
        except ImportError as exc:
            raise MemoryError(
                "LocalEmbeddingProvider needs the 'memory' extra: "
                "pip install agent-system[memory] "
                "(sentence-transformers). Falling back to HashEmbedding "
                "unless MEMORY_EMBEDDING_PROVIDER is reset to 'hash'."
            ) from exc
        self._model_name = model_name
        self._model = SentenceTransformer(model_name)

    def embed(self, text: str) -> list[float]:
        vec = self._model.encode(text, normalize_embeddings=True)
        return [float(v) for v in vec]


def embedding_provider_from_settings(settings: Any) -> EmbeddingProvider:
    """Select the embedding backend from settings (``hash`` default).

    Unknown provider names raise ``MemoryError`` (fail closed) rather than
    silently changing recall behavior.
    """
    name = str(getattr(settings, "memory_embedding_provider", "hash") or "hash").lower()
    if name == "hash":
        return HashEmbedding()
    if name == "local":
        return LocalEmbeddingProvider()
    raise MemoryError(f"unknown MEMORY_EMBEDDING_PROVIDER '{name}' (expected 'hash' or 'local')")


def build_memory_store(vault: ObsidianVaultWriter, settings: Any) -> MemoryStore:
    """Compose a MemoryStore with the settings-selected embedding backend."""
    return MemoryStore(vault, embedding_provider_from_settings(settings))


class MemoryStore:
    """Layered memory with secret filtering at write time.

    Records live in an in-process index; durable notes go through
    ObsidianVaultWriter. Secrets never enter: content is scrubbed before
    embedding or persistence.
    """

    def __init__(
        self,
        vault: ObsidianVaultWriter,
        embedder: EmbeddingProvider | None = None,
    ) -> None:
        self._vault = vault
        self._embedder = embedder or HashEmbedding()
        self._records: dict[str, MemoryRecord] = {}

    def remember(
        self,
        content: str,
        layer: MemoryLayer,
        source: str,
        session_id: str | None = None,
        task_id: str | None = None,
        workspace_id: str | None = None,
        tags: list[str] | None = None,
        links: list[str] | None = None,
        write_note: bool = True,
    ) -> MemoryRecord:
        content = scrub_text(content)
        memory_id = ids.new_memory_id()
        record = MemoryRecord(
            memory_id=memory_id,
            layer=layer,
            content=content,
            session_id=session_id,
            task_id=task_id,
            workspace_id=workspace_id,
            created_at=utcnow(),
            embedding=self._embedder.embed(content),
        )
        self._records[memory_id] = record
        if write_note:
            self._vault.write_note(
                NoteMeta(
                    title=f"{layer.value}: {content[:60]}",
                    layer=layer,
                    source=source,
                    task_id=task_id,
                    session_id=session_id,
                    tags=tags if tags is not None else [layer.value.lower(), source],
                    links=links or [],
                ),
                body=content,
            )
        return record

    def recall(
        self,
        query: str,
        layer: MemoryLayer | None = None,
        session_id: str | None = None,
        top_k: int = 5,
    ) -> list[tuple[MemoryRecord, float]]:
        """Rank memories by cosine similarity over embeddings."""
        q_vec = self._embedder.embed(query)
        scored: list[tuple[MemoryRecord, float]] = []
        for rec in self._records.values():
            if layer is not None and rec.layer != layer:
                continue
            if session_id is not None and rec.session_id != session_id:
                continue
            score = self._cosine(q_vec, rec.embedding or [])
            scored.append((rec, score))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:top_k]

    def forget(self, memory_id: str) -> bool:
        return self._records.pop(memory_id, None) is not None

    def forget_session(self, session_id: str) -> int:
        stale = [mid for mid, r in self._records.items() if r.session_id == session_id]
        for mid in stale:
            del self._records[mid]
        return len(stale)

    def all(self, layer: MemoryLayer | None = None) -> list[MemoryRecord]:
        return [r for r in self._records.values() if layer is None or r.layer == layer]

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b, strict=False))
        na = sum(x * x for x in a) ** 0.5 or 1.0
        nb = sum(y * y for y in b) ** 0.5 or 1.0
        return dot / (na * nb)


class DbNoteStore:
    """Database-backed vault for dyno (ephemeral-filesystem) deployments.

    Same scrubbed content as ``ObsidianVaultWriter`` (metadata via
    ``_scrub_meta``, body via ``scrub_text``), persisted to the
    ``memory_notes`` table instead of markdown files. Retrieval mirrors
    ``recall_recent`` keyword ranking (overlap, then newest first).
    """

    def __init__(self, factory: Any) -> None:
        self._factory = factory

    def write_note(self, meta: NoteMeta, body: str) -> str:
        from agent_system.domain import ids as _ids
        from agent_system.infra.db import session_scope as _scope
        from agent_system.infra.models import MemoryNote as _Row

        if not meta.title.strip():
            raise MemoryError("note title required")
        safe_meta = _scrub_meta(meta)
        clean_body = scrub_text(body)
        content = f"# {safe_meta.title}\n\n{clean_body}"
        if len(content.encode("utf-8")) > MAX_NOTE_BYTES:
            raise MemoryError(f"note exceeds {MAX_NOTE_BYTES} bytes")
        note_id = _ids.new_memory_id()
        layer_str = (
            safe_meta.layer.value if hasattr(safe_meta.layer, "value") else str(safe_meta.layer)
        )
        with _scope(self._factory) as db:
            db.add(
                _Row(
                    id=note_id,
                    title=safe_meta.title,
                    layer=layer_str,
                    source=safe_meta.source,
                    tags_json=list(safe_meta.tags or []),
                    links_json=list(safe_meta.links or []),
                    body=clean_body,
                    session_id=safe_meta.session_id,
                    task_id=safe_meta.task_id,
                    agent_run_id=safe_meta.agent_run_id,
                    owner_user_id=safe_meta.owner_user_id,
                )
            )
        return note_id

    def recall(
        self,
        query: str,
        limit: int = 3,
        owner_user_id: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        from agent_system.infra.db import session_scope as _scope
        from agent_system.infra.models import MemoryNote as _Row

        terms = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 2]
        with _scope(self._factory) as db:
            q = db.query(_Row)
            if owner_user_id is not None:
                q = q.filter(_Row.owner_user_id == str(owner_user_id))
            else:
                # Fail-closed: anonymous callers only see legacy unowned rows,
                # never another user's notes.
                q = q.filter(_Row.owner_user_id.is_(None))
            if session_id is not None:
                q = q.filter(_Row.session_id == str(session_id))
            rows = q.order_by(_Row.created_at.desc()).limit(500).all()
            scored = [
                (
                    sum((row.title + "\n" + row.body).lower().count(t) for t in terms)
                    if terms
                    else 0,
                    row,
                )
                for row in rows
            ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        out: list[dict[str, Any]] = []
        for overlap, row in scored[: max(limit, 0)]:
            if terms and overlap == 0 and len(out) >= 1:
                break
            out.append(
                {
                    "title": row.title,
                    "path": f"db:{row.id}",
                    "snippet": scrub_text(row.body[:600]),
                }
            )
        return out
