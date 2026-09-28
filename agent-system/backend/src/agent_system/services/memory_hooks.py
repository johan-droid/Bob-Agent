"""Memory hooks — keep the Obsidian vault growing without manual effort.

- ``remember_outcome``: called after a task succeeds; writes a scrubbed
  outcome note (never raises — memory must not break execution).
- ``remember_fact``: raises on failure (used by the memory_remember tool).
- ``recall_recent``: lexical recent-note retrieval for prompts and tools.
  Honest keyword ranking over real vault files — no fake embeddings.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def _note_owner(note: Path) -> str:
    """Read ``owner_user_id`` from a vault note's YAML frontmatter.

    Returns "" when absent or unparseable, so callers fail closed.
    """
    try:
        head = note.read_text(encoding="utf-8", errors="replace")[:2048]
    except OSError:
        return ""
    if not head.startswith("---"):
        return ""
    try:
        import yaml

        raw = head.split("---", 2)[1]
        data = yaml.safe_load(raw)
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("owner_user_id") or "")


def _store(settings: Any) -> Any:
    from agent_system.services.memory import ObsidianVaultWriter, build_memory_store

    vault_path = Path(str(getattr(settings, "vault_path", "vault"))).expanduser()
    vault = ObsidianVaultWriter(vault_path)
    try:
        return build_memory_store(vault, settings)
    except Exception:
        # Fail open to the honest lexical baseline (hash embeddings) when
        # the configured provider is unavailable — recall_recent file-scan
        # stays the live path and remember() still persists + scrubs.
        from agent_system.services.memory import HashEmbedding, MemoryStore

        return MemoryStore(vault, HashEmbedding())


def _use_db(settings: Any, factory: Any) -> bool:
    """True when the DB vault is enabled AND a factory is available.

    Local dev (flag off, or callers without a factory such as the CLI)
    keeps the Obsidian file vault untouched.
    """
    return bool(getattr(settings, "cloud_vault_db", False)) and factory is not None


def remember_fact(
    settings: Any,
    fact: str,
    *,
    session_id: str | None = None,
    task_id: str | None = None,
    tags: list[str] | None = None,
    factory: Any = None,
    owner_user_id: str | None = None,
) -> str:
    """Store one fact; returns the memory id (raises on failure)."""
    from agent_system.services.memory import DbNoteStore, MemoryLayer, NoteMeta

    if _use_db(settings, factory):
        return DbNoteStore(factory).write_note(
            NoteMeta(
                title=f"{MemoryLayer.TASK.value}: {fact[:60]}",
                layer=MemoryLayer.TASK,
                source="agent",
                session_id=session_id,
                task_id=task_id,
                owner_user_id=owner_user_id,
                tags=tags or ["agent"],
            ),
            body=fact,
        )
    store = _store(settings)
    record = store.remember(
        fact,
        MemoryLayer.TASK,
        source="agent",
        session_id=session_id,
        task_id=task_id,
        owner_user_id=owner_user_id,
        tags=tags or ["agent"],
    )
    return str(record.memory_id)


def remember_outcome(
    settings: Any,
    session_id: str,
    task_id: str,
    title: str,
    output: str,
    factory: Any = None,
    owner_user_id: str | None = None,
) -> str | None:
    """Persist a task outcome note. Never raises; None when disabled/failed."""
    try:
        if not bool(getattr(settings, "memory_auto_remember", True)):
            return None
        text = (output or "").strip()
        if not text:
            return None
        fact = f"Task '{title}' completed. Outcome: {text[:1500]}"
        return remember_fact(
            settings,
            fact,
            session_id=session_id,
            task_id=task_id,
            tags=["task-outcome"],
            factory=factory,
            owner_user_id=owner_user_id,
        )
    except Exception:
        return None


def recall_recent(
    settings: Any,
    query: str,
    limit: int = 3,
    factory: Any = None,
    owner_user_id: str | None = None,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Keyword-ranked recent vault notes (newest + most overlap first)."""
    if _use_db(settings, factory):
        from agent_system.services.memory import DbNoteStore

        try:
            return DbNoteStore(factory).recall(
                query, limit, owner_user_id=owner_user_id, session_id=session_id
            )
        except Exception:
            return []  # memory must never break execution
    from agent_system.services.memory import scrub_text

    vault = Path(str(getattr(settings, "vault_path", "vault"))).expanduser()
    if not vault.is_dir():
        return []  # Scope to the memory layers when present — the vault root also holds
    # spec mirrors (Agent_System/), SOUL notes, and dated journals that
    # must not be injected as "memories". Fall back to the root only when
    # no memory/ dir exists yet (fresh vault).
    memory_root = vault / "memory"
    scan_root = memory_root if memory_root.is_dir() else vault
    terms = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 2]
    scored: list[tuple[int, float, Path]] = []
    for note in scan_root.rglob("*.md"):
        try:
            if not note.is_file():
                continue
            # Per-user isolation. This path previously walked the whole vault
            # with no owner filter, so every user's notes were injected into
            # every user's prompt. When an owner is requested we fail closed:
            # a note with no recorded owner is not that user's memory.
            if owner_user_id and _note_owner(note) != owner_user_id:
                continue
            text = note.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lowered = text.lower()
        overlap = sum(lowered.count(term) for term in terms) if terms else 0
        try:
            mtime = note.stat().st_mtime
        except OSError:
            mtime = 0.0
        scored.append((overlap, mtime, note))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    out: list[dict[str, Any]] = []
    for overlap, _, note in scored[: max(limit, 0)]:
        if terms and overlap == 0 and len(out) >= 1:
            break
        try:
            body = note.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out.append(
            {
                "title": note.stem,
                "path": str(note),
                # Re-scrub on read (defense in depth — notes written before
                # write-time scrubbing, or edited by hand, stay safe).
                "snippet": scrub_text(body[:600]),
            }
        )
    return out


__all__ = ["recall_recent", "remember_fact", "remember_outcome"]
