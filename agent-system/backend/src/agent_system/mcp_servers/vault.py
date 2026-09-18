"""Bob Agent Vault MCP server — keeps the Obsidian vault updated.

Run as a stdio MCP server (JSON-RPC 2.0 over stdin/stdout) and attach it to
Bob through the existing ``MCP_SERVERS`` configuration::

    MCP_SERVERS='[{"name": "vault", "command": "uv",
                   "args": ["run", "bob-vault-mcp"],
                   "cwd": "agent-system/backend"}]'

Every capability it exposes therefore enters the canonical execution seam:
``mcp_list`` (discover) / ``mcp_call`` (execute tier → live per-call approval on
``mcp:vault:<tool>``) → registry → validation → policy → handler. The server
itself has no database, permission or event access.

Tools
-----
``vault_write_note``   write a memory-layer note (frontmatter + wiki-links)
``vault_append_daily`` append a timestamped line to ``daily/<YYYY-MM-DD>.md``
``vault_record``       update the dedicated **Bob Agent record** (upsert)
``vault_read_record``  read the Bob Agent record (path, counters, last events)
``vault_recall``       keyword-ranked search over the vault's memory notes
``vault_status``       vault root, note counts and record path

The Bob Agent record is a *separate* note (``records/bob-agent.md``,
``type: agent-record``) that is updated in place on every ``vault_record`` call:
its frontmatter counter/``updated`` stamp advance and one timestamped entry is
appended to ``## Activity``. The activity log is capped (newest kept) so a
long-lived Bob leaves a bounded, human-readable record instead of an
ever-growing file.

Safety, reusing the existing vault contract (never re-implemented here):
write-time secret scrubbing (``services.memory.scrub_text`` / ``_scrub_meta``),
the ``MAX_NOTE_BYTES`` size bound, and slug sanitisation all come from
``services.memory``. Writes are atomic (temp file + ``os.replace``) so a crash
or a concurrent update cannot leave a half-written note.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

PROTOCOL_VERSION = "2025-03-26"
SERVER_NAME = "bob-vault"
SERVER_VERSION = "0.1.0"

#: The dedicated, continuously-updated Bob Agent record (separate from memory
#: layers so it is never injected as a "memory" during recall).
RECORD_RELATIVE = Path("records") / "bob-agent.md"
RECORD_TYPE = "agent-record"
#: Bound on the appended activity log: keeps the newest entries only.
MAX_RECORD_EVENTS = 200
MAX_DAILY_LINE_CHARS = 4_000
_DAILY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_JSONRPC_VERSION = "2.0"
_PARSE_ERROR = -32700
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603


# ---------------------------------------------------------------------------
# Vault root resolution (single source of truth: Settings.vault_path)
# ---------------------------------------------------------------------------


def resolve_vault_root(override: str | None = None) -> Path:
    """Resolve the vault root: explicit override > ``VAULT_PATH`` > Settings.

    ``Settings.vault_path`` is the documented default, so an unattached server
    writes where the rest of Bob writes instead of inventing a second location.
    """
    if override:
        return Path(override).expanduser()
    env_value = os.environ.get("VAULT_PATH", "").strip()
    if env_value:
        return Path(env_value).expanduser()
    from agent_system.config import Settings

    return Path(Settings().vault_path).expanduser()


def _utcnow() -> datetime:
    """Timezone-aware UTC now (matches ``domain.events.utcnow`` semantics)."""
    return datetime.now(UTC)


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file + ``os.replace``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{id(text) & 0xFFFFFF:x}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass  # already replaced; a leftover temp file is not fatal


def _error(message: str) -> dict[str, Any]:
    """MCP tool result carrying a failure (visible to the caller, never silent)."""
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }


def _ok(payload: dict[str, Any], text: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload,
        "isError": False,
    }


def _as_str_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"'{field}' must be an array of strings")
    return [str(item) for item in value]


def _require_str(args: dict[str, Any], field: str) -> str:
    value = str(args.get(field) or "").strip()
    if not value:
        raise ValueError(f"'{field}' is required")
    return value


# ---------------------------------------------------------------------------
# Bob Agent record (separate note, updated in place)
# ---------------------------------------------------------------------------


def record_path(root: Path) -> Path:
    return root / RECORD_RELATIVE


def _parse_record(raw: str) -> tuple[dict[str, Any], list[str]]:
    """Split a record note into (frontmatter, activity bullets)."""
    if not raw.startswith("---"):
        return {}, [line for line in raw.splitlines() if line.startswith("- ")]
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, []
    try:
        frontmatter = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        frontmatter = {}
    if not isinstance(frontmatter, dict):
        frontmatter = {}
    bullets = [line for line in parts[2].splitlines() if line.startswith("- ")]
    return frontmatter, bullets


def _render_record(frontmatter: dict[str, Any], bullets: list[str]) -> str:
    fm_text = str(yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)).strip()
    activity = "\n".join(bullets)
    return (
        f"---\n{fm_text}\n---\n\n"
        f"# Bob Agent — Activity Record\n\n"
        f"Continuously updated by the `vault` MCP server (`bob-vault-mcp`). One\n"
        f"entry per recorded event, oldest first; the log keeps the newest\n"
        f"{MAX_RECORD_EVENTS} entries.\n\n"
        f"## Activity\n\n{activity}\n"
    )


def update_record(root: Path, event: str, details: str, tags: list[str]) -> dict[str, Any]:
    """Upsert the Bob Agent record: advance counters, append one entry."""
    from agent_system.services.memory import MAX_NOTE_BYTES, scrub_text

    path = record_path(root)
    event = scrub_text(event)
    details = scrub_text(details)
    now = _utcnow()
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    frontmatter, bullets = _parse_record(existing)
    created = str(frontmatter.get("created") or now.isoformat())
    events = int(frontmatter.get("events") or 0) + 1
    entry = f"- {now.isoformat()} — {event}" + (f" — {details}" if details else "")
    bullets.append(entry)
    bullets = bullets[-MAX_RECORD_EVENTS:]
    merged_tags = sorted({*(str(t) for t in (frontmatter.get("tags") or [])), *tags})
    frontmatter = {
        "type": RECORD_TYPE,
        "agent": "Bob Agent",
        "record": "bob-agent",
        "created": created,
        "updated": now.isoformat(),
        "events": events,
        "tags": merged_tags,
    }
    content = _render_record(frontmatter, bullets)
    if len(content.encode("utf-8")) > MAX_NOTE_BYTES:
        raise ValueError(f"record exceeds {MAX_NOTE_BYTES} bytes")
    _atomic_write(path, content)
    return {"path": str(path), "events": events, "entry": entry}


def read_record(root: Path, limit: int = 20) -> dict[str, Any]:
    path = record_path(root)
    if not path.is_file():
        return {"exists": False, "path": str(path), "events": 0, "entries": []}
    from agent_system.services.memory import scrub_text

    frontmatter, bullets = _parse_record(path.read_text(encoding="utf-8"))
    return {
        "exists": True,
        "path": str(path),
        "events": int(frontmatter.get("events") or 0),
        "created": frontmatter.get("created"),
        "updated": frontmatter.get("updated"),
        "entries": [scrub_text(line) for line in bullets[-max(limit, 0) :]],
    }


# ---------------------------------------------------------------------------
# Tool handlers (all vault writes reuse services.memory's scrubbing + bounds)
# ---------------------------------------------------------------------------


def _handle_write_note(root: Path, args: dict[str, Any]) -> dict[str, Any]:
    from agent_system.services.memory import MemoryLayer, NoteMeta, ObsidianVaultWriter

    title = _require_str(args, "title")
    body = _require_str(args, "body")
    layer_raw = str(args.get("layer") or MemoryLayer.TASK.value).strip().upper()
    try:
        layer = MemoryLayer(layer_raw)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in MemoryLayer)
        raise ValueError(f"unknown layer '{layer_raw}' (expected one of: {allowed})") from exc
    writer = ObsidianVaultWriter(root)
    path = writer.write_note(
        NoteMeta(
            title=title,
            layer=layer,
            source=str(args.get("source") or "bob-vault-mcp"),
            tags=_as_str_list(args.get("tags"), "tags"),
            links=_as_str_list(args.get("links"), "links"),
        ),
        body,
    )
    return _ok(
        {"path": str(path), "layer": layer.value, "title": title},
        f"note written: {path}",
    )


def _handle_append_daily(root: Path, args: dict[str, Any]) -> dict[str, Any]:
    from agent_system.services.memory import MAX_NOTE_BYTES, scrub_text

    text = scrub_text(_require_str(args, "text"))[:MAX_DAILY_LINE_CHARS]
    date_raw = str(args.get("date") or "").strip()
    if date_raw and not _DAILY_RE.match(date_raw):
        raise ValueError("'date' must be YYYY-MM-DD")
    now = _utcnow()
    day = date_raw or now.strftime("%Y-%m-%d")
    path = root / "daily" / f"{day}.md"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if existing:
        content = existing.rstrip("\n") + f"\n- {now.strftime('%H:%M:%S')} — {text}\n"
    else:
        content = (
            f"---\ntype: daily-log\ndate: {day}\n---\n\n"
            f"# Daily log — {day}\n\n- {now.strftime('%H:%M:%S')} — {text}\n"
        )
    if len(content.encode("utf-8")) > MAX_NOTE_BYTES:
        raise ValueError(f"daily note exceeds {MAX_NOTE_BYTES} bytes")
    _atomic_write(path, content)
    return _ok({"path": str(path), "date": day}, f"daily entry appended: {path}")


def _handle_record(root: Path, args: dict[str, Any]) -> dict[str, Any]:
    event = _require_str(args, "event")
    details = str(args.get("details") or "")
    tags = _as_str_list(args.get("tags"), "tags") or ["bob-agent"]
    result = update_record(root, event, details, tags)
    return _ok(result, f"Bob Agent record updated ({result['events']} events): {result['path']}")


def _handle_read_record(root: Path, args: dict[str, Any]) -> dict[str, Any]:
    limit = int(args.get("limit") or 20)
    if limit < 1 or limit > MAX_RECORD_EVENTS:
        raise ValueError(f"'limit' must be between 1 and {MAX_RECORD_EVENTS}")
    result = read_record(root, limit)
    if not result["exists"]:
        return _ok(result, f"no Bob Agent record yet at {result['path']}")
    return _ok(result, f"Bob Agent record: {result['events']} events at {result['path']}")


def _handle_recall(root: Path, args: dict[str, Any]) -> dict[str, Any]:
    from agent_system.services.memory_hooks import recall_recent

    query = _require_str(args, "query")
    limit = int(args.get("limit") or 3)
    if limit < 1 or limit > 20:
        raise ValueError("'limit' must be between 1 and 20")
    notes = recall_recent(SimpleNamespace(vault_path=str(root)), query, limit)
    return _ok(
        {"query": query, "count": len(notes), "notes": notes},
        f"{len(notes)} note(s) matched '{query}'",
    )


def _handle_status(root: Path, args: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG001
    from agent_system.services.memory import MemoryLayer, ObsidianVaultWriter

    writer = ObsidianVaultWriter(root)
    layers = {layer.value: len(writer.list_notes(layer)) for layer in MemoryLayer}
    daily_dir = root / "daily"
    record = read_record(root, limit=1)
    payload = {
        "vault": str(root),
        "exists": root.is_dir(),
        "layers": layers,
        "notes": sum(layers.values()),
        "daily_logs": len(list(daily_dir.glob("*.md"))) if daily_dir.is_dir() else 0,
        "record_path": str(record_path(root)),
        "record_exists": bool(record["exists"]),
        "record_events": int(record["events"]),
    }
    return _ok(payload, f"vault {root}: {payload['notes']} memory notes")


_Handler = Callable[[Path, dict[str, Any]], dict[str, Any]]

_HANDLERS: dict[str, _Handler] = {
    "vault_write_note": _handle_write_note,
    "vault_append_daily": _handle_append_daily,
    "vault_record": _handle_record,
    "vault_read_record": _handle_read_record,
    "vault_recall": _handle_recall,
    "vault_status": _handle_status,
}


def _call_tool(root: Path, name: str, args: dict[str, Any]) -> dict[str, Any]:
    handler = _HANDLERS.get(name)
    if handler is None:
        return _error(f"unknown tool '{name}'")
    try:
        return handler(root, args)
    except ValueError as exc:
        return _error(f"{name}: {exc}")
    except Exception as exc:  # vault failures are reported, never fatal
        return _error(f"{name} failed: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# MCP tool catalog (JSON Schema; the client bounds/shapes it — never authority)
# ---------------------------------------------------------------------------

_STR = {"type": "string"}
_STR_ARRAY = {"type": "array", "items": {"type": "string"}}
_LAYERS = ["SYSTEM", "USER", "TASK", "WORKSPACE"]

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "vault_write_note",
        "description": (
            "Write one markdown note into the Obsidian vault (YAML frontmatter + "
            "wiki-links, secrets scrubbed). Layer is one of SYSTEM/USER/TASK/WORKSPACE."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {**_STR, "description": "Note title"},
                "body": {**_STR, "description": "Note body (markdown)"},
                "layer": {"type": "string", "enum": _LAYERS, "default": "TASK"},
                "tags": _STR_ARRAY,
                "links": {**_STR_ARRAY, "description": "Wiki-link targets"},
                "source": {**_STR, "description": "What produced this note"},
            },
            "required": ["title", "body"],
            "additionalProperties": False,
        },
    },
    {
        "name": "vault_append_daily",
        "description": "Append a timestamped line to the vault's daily log (daily/<date>.md).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {**_STR, "description": "Line to append"},
                "date": {**_STR, "description": "YYYY-MM-DD (default: today, UTC)"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "vault_record",
        "description": (
            "Update the dedicated Bob Agent record (records/bob-agent.md): bumps the "
            "event counter and appends one timestamped entry to its Activity log."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "event": {**_STR, "description": "Short event name (e.g. release-gate)"},
                "details": {**_STR, "description": "Optional detail line"},
                "tags": _STR_ARRAY,
            },
            "required": ["event"],
            "additionalProperties": False,
        },
    },
    {
        "name": "vault_read_record",
        "description": "Read the Bob Agent record: path, counters and the newest entries.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RECORD_EVENTS}
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "vault_recall",
        "description": "Keyword-rank the vault's memory notes for a query.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {**_STR, "description": "Search text"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "vault_status",
        "description": "Report the vault root, per-layer note counts and the record path.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 stdio loop
# ---------------------------------------------------------------------------


def _response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": _JSONRPC_VERSION, "id": request_id, "result": result}


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": _JSONRPC_VERSION,
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def handle_message(root: Path, message: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one JSON-RPC message; ``None`` means "notification, no reply"."""
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params")
    params = params if isinstance(params, dict) else {}
    if request_id is None:
        return None  # notification (e.g. notifications/initialized)
    if method == "initialize":
        return _response(
            request_id,
            {
                "protocolVersion": str(params.get("protocolVersion") or PROTOCOL_VERSION),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    if method == "ping":
        return _response(request_id, {})
    if method == "tools/list":
        return _response(request_id, {"tools": TOOL_SCHEMAS})
    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments")
        if not name or (arguments is not None and not isinstance(arguments, dict)):
            return _rpc_error(
                request_id, _INVALID_PARAMS, "tools/call needs name + object arguments"
            )
        return _response(request_id, _call_tool(root, name, arguments or {}))
    return _rpc_error(request_id, _METHOD_NOT_FOUND, f"unknown method '{method}'")


def serve(root: Path, stdin: Any = None, stdout: Any = None) -> int:
    """Read newline-delimited JSON-RPC from stdin until EOF; always return 0."""
    in_stream = stdin if stdin is not None else sys.stdin
    out_stream = stdout if stdout is not None else sys.stdout
    for line in in_stream:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            message = json.loads(stripped)
        except json.JSONDecodeError:
            reply: dict[str, Any] | None = _rpc_error(None, _PARSE_ERROR, "invalid JSON")
        else:
            if not isinstance(message, dict):
                reply = _rpc_error(None, _INVALID_PARAMS, "message must be a JSON object")
            else:
                try:
                    reply = handle_message(root, message)
                except Exception as exc:  # never crash the transport loop
                    reply = _rpc_error(
                        message.get("id"), _INTERNAL_ERROR, f"{type(exc).__name__}: {exc}"
                    )
        if reply is not None:
            out_stream.write(json.dumps(reply, ensure_ascii=False) + "\n")
            out_stream.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bob-vault-mcp",
        description="Bob Agent Obsidian-vault MCP server (stdio JSON-RPC).",
    )
    parser.add_argument(
        "--vault",
        default=None,
        help="Vault root (default: VAULT_PATH env, else the app's configured vault).",
    )
    args = parser.parse_args(argv)
    return serve(resolve_vault_root(args.vault))


__all__ = [
    "MAX_RECORD_EVENTS",
    "RECORD_RELATIVE",
    "SERVER_NAME",
    "TOOL_SCHEMAS",
    "handle_message",
    "main",
    "read_record",
    "record_path",
    "resolve_vault_root",
    "serve",
    "update_record",
]


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
