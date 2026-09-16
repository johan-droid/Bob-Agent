"""Agent soul — loads SOUL.md (Bob's identity) for prompt injection.

The soul is the `<identity>` block prepended to every model call when the
router is built with it (see ``build_model_router(soul_text=...)``). Lookup
order: explicit path > ``SOUL_PATH`` setting > ``./SOUL.md`` > parent dir
``SOUL.md`` (repo root when running from ``backend/``). Missing file is not
an error — the router simply sends no identity block.
"""

from __future__ import annotations

from pathlib import Path

FILENAME = "SOUL.md"


def find_soul(explicit: str | Path | None = None) -> Path | None:
    """Locate the soul file, or None when there is none.

    An explicit path that does not exist is a miss (no silent fallback);
    otherwise search ./SOUL.md, then parent-dir SOUL.md.
    """
    if explicit:
        candidate = Path(explicit).expanduser()
        try:
            return candidate if candidate.is_file() else None
        except OSError:
            return None
    cwd = Path.cwd()
    for candidate in (cwd / FILENAME, cwd.parent / FILENAME):
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def load_soul(explicit: str | Path | None = None) -> tuple[Path | None, str]:
    """Return (path, text); empty text when no soul file exists (never raises)."""
    path = find_soul(explicit)
    if path is None:
        return None, ""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None, ""
    return path, text


def identity_block(soul_text: str) -> str:
    """Wrap soul markdown as the `<identity>` prompt block."""
    return f"<identity>\n{soul_text.strip()}\n</identity>"
