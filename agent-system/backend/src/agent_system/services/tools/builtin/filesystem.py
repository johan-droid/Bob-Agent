"""Filesystem capabilities (v3.1 §14).

Structured file operations: read, write, list, search, edit, patch, diff,
directory tree, metadata. Every path goes through the workspace jail in
``services/tools/paths.py`` — symlinks are resolved before containment is
checked and secret paths are always refused.

Risk: reads are ``read``; every mutation is ``write`` and therefore runs behind
the permission gate. There is no destructive tier here: nothing in this group
deletes or truncates by design.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from pathlib import Path
from typing import Any

from agent_system.services.tool_errors import ToolError
from agent_system.services.tools.builtin._exec import MAX_OUTPUT_CHARS, read_text
from agent_system.services.tools.paths import (
    allowed_roots,
    jailed,
    max_file_bytes,
    relative_label,
)
from agent_system.services.tools.registry import Tool, ToolContext, ToolRegistry, _str_param

GROUP = "filesystem"

#: Directories never descended into by search/tree capabilities.
_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".next",
        ".cache",
    }
)
MAX_SEARCH_RESULTS = 200
MAX_TREE_ENTRIES = 500
MAX_PATCH_BYTES = 2_000_000


def _top_string_param(description: str) -> dict[str, Any]:
    return _str_param(description)


def _file_read(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    target = jailed(str(args.get("path") or ""), ctx.settings, must_exist=True)
    if target.is_dir():
        raise ToolError(f"is a directory, use file_list: {args.get('path')}")
    raw = read_text(target, ctx)
    try:
        return {
            "path": relative_label(target),
            "content": raw,
            "lines": len(raw.splitlines()),
            "bytes": target.stat().st_size,
        }
    except OSError as exc:  # pragma: no cover — stat after a successful read
        raise ToolError(f"cannot stat {target.name}: {exc}") from exc


def _file_write(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    target = jailed(str(args.get("path") or ""), ctx.settings)
    content = str(args.get("content") or "")
    encoded = content.encode("utf-8")
    cap = max_file_bytes(ctx.settings)
    if len(encoded) > cap:
        raise ToolError(f"content exceeds max_file_size_mb ({len(encoded)} bytes)")
    created = not target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {
        "path": relative_label(target),
        "bytes": len(encoded),
        "created": created,
        "lines": len(content.splitlines()),
    }


def _file_list(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    target = jailed(str(args.get("path") or "."), ctx.settings, must_exist=True)
    if not target.is_dir():
        raise ToolError(f"not a directory: {args.get('path')}")
    entries: list[dict[str, Any]] = []
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if child.name in _IGNORED_DIRS:
            continue
        try:
            stat = child.stat()
        except OSError:
            continue
        entries.append(
            {
                "name": child.name,
                "type": "dir" if child.is_dir() else "file",
                "size": stat.st_size if child.is_file() else None,
            }
        )
    return {"path": relative_label(target), "count": len(entries), "entries": entries[:200]}


def _iter_files(root: Path, *, pattern: str, limit: int) -> list[Path]:
    matcher = re.compile(pattern)
    found: list[Path] = []
    for path in root.rglob("*"):
        if any(part in _IGNORED_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        if not matcher.search(path.name):
            continue
        found.append(path)
        if len(found) >= limit:
            break
    return found


def _file_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    root = jailed(str(args.get("path") or "."), ctx.settings, must_exist=True)
    if not root.is_dir():
        raise ToolError("file_search expects a directory")
    query = str(args.get("query") or "")
    if not query:
        raise ToolError("file_search: 'query' is required")
    try:
        needle = re.compile(query)
    except re.error as exc:
        raise ToolError(f"invalid regular expression: {exc}") from exc
    name_pattern = str(args.get("name_pattern") or ".")
    max_results = min(int(args.get("max_results") or 50), MAX_SEARCH_RESULTS)
    matches: list[dict[str, Any]] = []
    for path in _iter_files(root, pattern=name_pattern, limit=MAX_SEARCH_RESULTS):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if needle.search(line):
                matches.append(
                    {
                        "path": relative_label(path),
                        "line": number,
                        "text": line.strip()[:300],
                    }
                )
                if len(matches) >= max_results:
                    break
        if len(matches) >= max_results:
            break
    return {"query": query, "count": len(matches), "matches": matches}


def _file_edit(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    target = jailed(str(args.get("path") or ""), ctx.settings, must_exist=True)
    old = args.get("old") if "old" in args else args.get("find")
    new = args.get("new") if "new" in args else args.get("replace")
    if not isinstance(old, str) or not old:
        raise ToolError("file_edit: 'old' text is required")
    if new is None:
        raise ToolError("file_edit: 'new' text is required (use \"\" to delete)")
    if not isinstance(new, str):
        raise ToolError("file_edit: 'new' must be a string")
    text = read_text(target, ctx)
    occurrences = text.count(old)
    if occurrences == 0:
        raise ToolError("file_edit: 'old' text was not found (no change written)")
    if occurrences > 1 and not bool(args.get("replace_all")):
        raise ToolError(
            f"file_edit: 'old' text appears {occurrences} times; pass replace_all=true "
            "or include more surrounding context"
        )
    replace_all = bool(args.get("replace_all"))
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    target.write_text(updated, encoding="utf-8")
    return {
        "path": relative_label(target),
        "replacements": occurrences if replace_all else 1,
        "diff": _unified_diff(text, updated, relative_label(target)),
    }


def _unified_diff(before: str, after: str, label: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{label}",
            tofile=f"b/{label}",
        )
    )[:MAX_OUTPUT_CHARS]


def _file_patch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Apply a unified diff to one file — all hunks or nothing."""
    target = jailed(str(args.get("path") or ""), ctx.settings, must_exist=True)
    patch = str(args.get("patch") or "")
    if not patch.strip():
        raise ToolError("file_patch: 'patch' unified diff text is required")
    if len(patch) > MAX_PATCH_BYTES:
        raise ToolError("file_patch: patch is too large")
    original = read_text(target, ctx)
    updated = apply_unified_diff(original, patch)
    label = relative_label(target)
    if updated == original:
        return {"path": label, "changed": False, "diff": ""}
    target.write_text(updated, encoding="utf-8")
    return {
        "path": label,
        "changed": True,
        "diff": _unified_diff(original, updated, label),
    }


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


def parse_unified_diff(patch: str) -> list[tuple[int, list[str]]]:
    """Parse a unified diff into (old_start, body_lines) hunks.

    Body lines keep their ``-``/``+``/space prefix so the applier can match
    context exactly. Raises :class:`ToolError` on a malformed patch.
    """
    headers = list(_HUNK_RE.finditer(patch))
    if not headers:
        raise ToolError("file_patch: no '@@ -l,c +l,c @@' hunk headers found")
    hunks: list[tuple[int, list[str]]] = []
    for index, match in enumerate(headers):
        start = int(match.group(1)) - 1
        end = headers[index + 1].start() if index + 1 < len(headers) else len(patch)
        body = patch[match.end() : end].splitlines()
        cleaned = [line for line in body if line and line[0] in " -+" and not line.startswith("\\")]
        if not cleaned:
            raise ToolError("file_patch: hunk contains no changed lines")
        hunks.append((start, cleaned))
    return hunks


def apply_unified_diff(original: str, patch: str) -> str:
    """Apply every hunk, verifying context first; raise if any hunk mismatches."""
    lines = original.splitlines()
    trailing_newline = original.endswith("\n")
    hunks = parse_unified_diff(patch)
    offset = 0
    for start, body in hunks:
        expected = [line[1:] for line in body if line[0] in " -"]
        replacement = [line[1:] for line in body if line[0] in " +"]
        position = max(0, start + offset)
        if lines[position : position + len(expected)] != expected:
            # Fall back to a context search so patches that are valid but whose
            # line numbers drifted still apply; ambiguity is a hard error.
            candidate = _find_unique(lines, expected)
            if candidate is None:
                raise ToolError(
                    "file_patch: hunk context did not match the file (no partial writes made)"
                )
            position = candidate
        lines[position : position + len(expected)] = replacement
        offset += len(replacement) - len(expected)
    result = "\n".join(lines)
    return result + "\n" if trailing_newline else result


def _find_unique(lines: list[str], expected: list[str]) -> int | None:
    if not expected:
        return None
    hits = [
        index
        for index in range(0, len(lines) - len(expected) + 1)
        if lines[index : index + len(expected)] == expected
    ]
    return hits[0] if len(hits) == 1 else None


def _file_diff(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    left = jailed(str(args.get("path") or ""), ctx.settings, must_exist=True)
    right_raw = str(args.get("other") or args.get("path_b") or "")
    if right_raw:
        right = jailed(right_raw, ctx.settings, must_exist=True)
    else:
        right = jailed(str(args.get("path") or ""), ctx.settings, must_exist=True)
    before = read_text(left, ctx)
    after = read_text(right, ctx) if right_raw else ""
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=relative_label(left),
            tofile=relative_label(right) if right_raw else "(empty)",
        )
    )
    return {
        "path": relative_label(left),
        "other": relative_label(right) if right_raw else None,
        "changed": before != after,
        "diff": diff[:MAX_OUTPUT_CHARS],
    }


def _directory_tree(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    root = jailed(str(args.get("path") or "."), ctx.settings, must_exist=True)
    if not root.is_dir():
        raise ToolError("directory_tree expects a directory")
    max_depth = min(int(args.get("max_depth") or 3), 10)
    entries: list[str] = []

    def walk(directory: Path, prefix: str, depth: int) -> None:
        if depth > max_depth or len(entries) >= MAX_TREE_ENTRIES:
            return
        children = sorted(
            (c for c in directory.iterdir() if c.name not in _IGNORED_DIRS),
            key=lambda p: (not p.is_dir(), p.name.lower()),
        )
        for child in children:
            if len(entries) >= MAX_TREE_ENTRIES:
                return
            suffix = "/" if child.is_dir() else ""
            entries.append(f"{prefix}{child.name}{suffix}")
            if child.is_dir():
                walk(child, prefix + "  ", depth + 1)

    walk(root, "", 1)
    return {
        "path": relative_label(root),
        "max_depth": max_depth,
        "truncated": len(entries) >= MAX_TREE_ENTRIES,
        "tree": entries,
    }


def _file_metadata(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    target = jailed(str(args.get("path") or ""), ctx.settings, must_exist=True)
    try:
        stat = target.stat()
    except OSError as exc:
        raise ToolError(f"cannot stat: {exc}") from exc
    info: dict[str, Any] = {
        "path": relative_label(target),
        "exists": True,
        "is_dir": target.is_dir(),
        "is_symlink": target.is_symlink(),
        "size_bytes": stat.st_size if target.is_file() else None,
        "modified_at": stat.st_mtime,
        "mode": oct(stat.st_mode & 0o777),
    }
    if target.is_file():
        digest = hashlib.sha256()
        binary = False
        with target.open("rb") as handle:
            while chunk := handle.read(65536):
                digest.update(chunk)
                if b"\x00" in chunk:
                    binary = True
        info["sha256"] = digest.hexdigest()
        info["binary"] = binary
    return info


def register(registry: ToolRegistry, settings: Any = None) -> None:  # noqa: ARG001
    registry.register(
        Tool(
            name="file_read",
            description="Read a UTF-8 text file under allowed roots.",
            parameters={
                "type": "object",
                "properties": {"path": _top_string_param("File path to read")},
                "required": ["path"],
                "additionalProperties": False,
            },
            risk="read",
            handler=_file_read,
            group=GROUP,
        )
    )
    registry.register(
        Tool(
            name="file_write",
            description="Write a text file under allowed roots (creates parents).",
            parameters={
                "type": "object",
                "properties": {
                    "path": _str_param("File path to write"),
                    "content": _str_param("Full file content"),
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            risk="write",
            handler=_file_write,
            scope="file:write",
            group=GROUP,
        )
    )
    registry.register(
        Tool(
            name="file_list",
            description="List a directory under allowed roots.",
            parameters={
                "type": "object",
                "properties": {"path": _str_param("Directory (default .)")},
                "required": [],
                "additionalProperties": False,
            },
            risk="read",
            handler=_file_list,
            group=GROUP,
        )
    )
    registry.register(
        Tool(
            name="file_search",
            description="Search file contents under a directory (regex), with name filtering.",
            parameters={
                "type": "object",
                "properties": {
                    "query": _str_param("Regular expression to find in file contents"),
                    "path": _str_param("Directory to search (default .)"),
                    "name_pattern": _str_param("Regex the file name must match (default all)"),
                    "max_results": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_RESULTS},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            risk="read",
            handler=_file_search,
            group=GROUP,
        )
    )
    registry.register(
        Tool(
            name="file_edit",
            description=(
                "Replace exact text in an existing file. Fails if the text is absent, "
                "and refuses an ambiguous match unless replace_all is true."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": _str_param("File to edit"),
                    "old": _str_param("Exact text to replace"),
                    "new": _str_param("Replacement text (empty string deletes)"),
                    "replace_all": {"type": "boolean", "description": "Replace every match"},
                },
                "required": ["path", "old", "new"],
                "additionalProperties": False,
            },
            risk="write",
            handler=_file_edit,
            scope="file:write",
            group=GROUP,
        )
    )
    registry.register(
        Tool(
            name="file_patch",
            description=(
                "Apply a unified diff (@@ hunks) to a file. All hunks must apply or "
                "nothing is written."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": _str_param("File to patch"),
                    "patch": _str_param("Unified diff text with @@ hunk headers"),
                },
                "required": ["path", "patch"],
                "additionalProperties": False,
            },
            risk="write",
            handler=_file_patch,
            scope="file:write",
            group=GROUP,
        )
    )
    registry.register(
        Tool(
            name="file_diff",
            description="Unified diff between two files (omit other to diff against empty).",
            parameters={
                "type": "object",
                "properties": {
                    "path": _str_param("First file"),
                    "other": _str_param("Second file (default: diff first against empty)"),
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            risk="read",
            handler=_file_diff,
            group=GROUP,
        )
    )
    registry.register(
        Tool(
            name="directory_tree",
            description="Recursive directory listing as indented text (bounded depth).",
            parameters={
                "type": "object",
                "properties": {
                    "path": _str_param("Root directory (default .)"),
                    "max_depth": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": [],
                "additionalProperties": False,
            },
            risk="read",
            handler=_directory_tree,
            group=GROUP,
        )
    )
    registry.register(
        Tool(
            name="file_metadata",
            description="Size, mode, mtime, line/binary detection and sha256 for a path.",
            parameters={
                "type": "object",
                "properties": {"path": _str_param("Path to inspect")},
                "required": ["path"],
                "additionalProperties": False,
            },
            risk="read",
            handler=_file_metadata,
            group=GROUP,
        )
    )


__all__ = ["GROUP", "apply_unified_diff", "allowed_roots", "parse_unified_diff", "register"]
