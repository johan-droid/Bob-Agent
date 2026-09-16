"""Coding capabilities (v3.1 §14).

Coding must not depend on an agent improvising shell commands. This group makes
the coding loop explicit:

- understand: ``project_detect``, ``repo_search``, ``read_source``
- change:     ``edit_source``, ``apply_patch``
- verify:     ``run_tests``, ``run_linter``, ``run_typecheck``, ``run_formatter``
- inspect:    ``inspect_dependencies``, ``inspect_build``

Project detection reads the workspace's own manifest files, so the verify
capabilities run the project's real commands rather than a guess. When nothing
is detected they fail with the reason instead of inventing a command.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agent_system.services.tool_errors import ToolError
from agent_system.services.tools.builtin._exec import (
    read_text,
    run_workspace_command,
)
from agent_system.services.tools.paths import jailed, relative_label
from agent_system.services.tools.registry import Tool, ToolContext, ToolRegistry, _str_param

GROUP = "coding"

#: marker file -> (language, ecosystem, test cmd, lint cmd, typecheck cmd, format cmd)
_PROJECT_MARKERS: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        "pyproject.toml",
        {
            "language": "python",
            "package_manager": "uv",
            "test": "uv run pytest -q",
            "lint": "uv run ruff check .",
            "typecheck": "uv run mypy .",
            "format": "uv run ruff format .",
            "dependencies_from": "pyproject.toml",
        },
    ),
    (
        "package.json",
        {
            "language": "typescript",
            "package_manager": "npm",
            "test": "npm test",
            "lint": "npm run lint",
            "typecheck": "npx tsc --noEmit",
            "format": "npx prettier --write .",
            "dependencies_from": "package.json",
        },
    ),
    (
        "Cargo.toml",
        {
            "language": "rust",
            "package_manager": "cargo",
            "test": "cargo test",
            "lint": "cargo clippy",
            "typecheck": "cargo check",
            "format": "cargo fmt",
            "dependencies_from": "Cargo.toml",
        },
    ),
    (
        "go.mod",
        {
            "language": "go",
            "package_manager": "go",
            "test": "go test ./...",
            "lint": "go vet ./...",
            "typecheck": "go build ./...",
            "format": "gofmt -w .",
            "dependencies_from": "go.mod",
        },
    ),
    (
        "Makefile",
        {
            "language": "make",
            "package_manager": None,
            "test": "make test",
            "lint": None,
            "typecheck": None,
            "format": None,
            "dependencies_from": None,
        },
    ),
)

_SOURCE_SUFFIXES = frozenset(
    {
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".go",
        ".rs",
        ".java",
        ".rb",
        ".c",
        ".h",
        ".cpp",
        ".cs",
        ".sh",
        ".sql",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
        ".md",
    }
)

_IGNORED_DIRS = frozenset(
    {
        ".git",
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
    }
)
MAX_RESULTS = 200


def detect_project(root: Path) -> dict[str, Any]:
    """Detect the project layout and the commands its own tooling defines."""
    found: list[dict[str, Any]] = []
    for marker, info in _PROJECT_MARKERS:
        path = root / marker
        if not path.is_file():
            continue
        entry = {"marker": marker, **info}
        if marker == "Makefile":
            entry = {**entry, **{k: v for k, v in _makefile_targets(path).items() if v}}
        found.append(entry)
    languages = sorted({str(entry["language"]) for entry in found})
    primary = found[0] if found else None
    return {
        "root": relative_label(root),
        "detected": bool(found),
        "languages": languages,
        "projects": found,
        "commands": (
            {
                "test": primary.get("test"),
                "lint": primary.get("lint"),
                "typecheck": primary.get("typecheck"),
                "format": primary.get("format"),
            }
            if primary
            else {}
        ),
        "hint": (
            None
            if found
            else "no known project marker found (pyproject.toml, package.json, Cargo.toml, "
            "go.mod, Makefile)"
        ),
    }


def _makefile_targets(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    targets = {
        match.group(1) for match in re.finditer(r"^([A-Za-z0-9_-]+):(?!=)", text, re.MULTILINE)
    }
    return {
        "test": "make test" if "test" in targets else "",
        "lint": "make lint" if "lint" in targets else "",
        "typecheck": "make typecheck" if "typecheck" in targets else "",
        "format": "make format" if "format" in targets else "",
    }


def _project_detect(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    root = jail_dir(args, ctx)
    return detect_project(root)


def jail_dir(args: dict[str, Any], ctx: ToolContext) -> Path:
    target = jailed(str(args.get("path") or args.get("repo") or "."), ctx.settings, must_exist=True)
    if not target.is_dir():
        raise ToolError(f"not a directory: {args.get('path') or args.get('repo')}")
    return target


def _repo_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    root = jail_dir(args, ctx)
    query = str(args.get("query") or "")
    if not query:
        raise ToolError("repo_search: 'query' is required")
    try:
        needle = re.compile(query)
    except re.error as exc:
        raise ToolError(f"invalid regular expression: {exc}") from exc
    language = str(args.get("language") or "")
    max_results = min(int(args.get("max_results") or 50), MAX_RESULTS)
    matches: list[dict[str, Any]] = []
    for path in root.rglob("*"):
        if any(part in _IGNORED_DIRS for part in path.parts) or not path.is_file():
            continue
        if path.suffix not in _SOURCE_SUFFIXES:
            continue
        if language and path.suffix != f".{language.lstrip('.')}":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if needle.search(line):
                matches.append(
                    {"path": relative_label(path), "line": number, "text": line.strip()[:300]}
                )
                if len(matches) >= max_results:
                    break
        if len(matches) >= max_results:
            break
    return {"query": query, "count": len(matches), "matches": matches}


def _read_source(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    target = jailed(str(args.get("path") or ""), ctx.settings, must_exist=True)
    if target.suffix not in _SOURCE_SUFFIXES:
        raise ToolError(
            f"read_source expects a source file; '{target.suffix or target.name}' is not one "
            "(use file_read for arbitrary text)"
        )
    start = max(1, int(args.get("start_line") or 1))
    end = args.get("end_line")
    text = read_text(target, ctx)
    lines = text.splitlines()
    stop = min(len(lines), int(end)) if end is not None else len(lines)
    if stop < start:
        raise ToolError(f"end_line ({stop}) is before start_line ({start})")
    numbered = [
        f"{number:>5} | {line}" for number, line in enumerate(lines[start - 1 : stop], start=start)
    ]
    return {
        "path": relative_label(target),
        "start_line": start,
        "end_line": stop,
        "total_lines": len(lines),
        "content": "\n".join(numbered),
    }


def _edit_source(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from agent_system.services.tools.builtin.filesystem import _file_edit

    target = jailed(str(args.get("path") or ""), ctx.settings, must_exist=True)
    if target.suffix not in _SOURCE_SUFFIXES:
        raise ToolError(f"edit_source expects a source file, got '{target.suffix}'")
    result = _file_edit(args, ctx)
    return {**result, "kind": "source_edit"}


def _apply_patch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from agent_system.services.tools.builtin.filesystem import _file_patch

    return {**_file_patch(args, ctx), "kind": "source_patch"}


def _verify_command(kind: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Run the project's own test/lint/typecheck/format command."""
    root = jail_dir(args, ctx)
    explicit = str(args.get("command") or "").strip()
    project = detect_project(root)
    command = explicit or str((project.get("commands") or {}).get(kind) or "")
    if not command:
        raise ToolError(
            f"{kind}: no {'command' if not explicit else ''} detected for this project "
            f"(hint: {project.get('hint') or 'pass an explicit command'})"
        )
    result = run_workspace_command(ctx, command, cwd=relative_label(root))
    return {
        "kind": kind,
        "command": command,
        "exit_code": result.get("exit_code"),
        "ok": result.get("exit_code") == 0,
        "output": result.get("output"),
        "mode": result.get("mode"),
    }


def _run_tests(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return _verify_command("test", args, ctx)


def _run_linter(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return _verify_command("lint", args, ctx)


def _run_typecheck(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return _verify_command("typecheck", args, ctx)


def _run_formatter(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return _verify_command("format", args, ctx)


def _inspect_dependencies(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    root = jail_dir(args, ctx)
    dependencies: dict[str, list[str]] = {}
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        dependencies["python"] = _toml_dependency_names(pyproject, ctx)
    package = root / "package.json"
    if package.is_file():
        try:
            data = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ToolError(f"package.json is not valid JSON: {exc}") from exc
        names = list((data.get("dependencies") or {}).keys())
        names += list((data.get("devDependencies") or {}).keys())
        dependencies["node"] = sorted(names)
    requirements = root / "requirements.txt"
    if requirements.is_file():
        text = requirements.read_text(encoding="utf-8", errors="replace")
        dependencies.setdefault("python", []).extend(
            line.split("==")[0].strip()
            for line in text.splitlines()
            if line.strip() and not line.startswith("#")
        )
    if not dependencies:
        raise ToolError("inspect_dependencies: no pyproject.toml, package.json or requirements.txt")
    return {
        "root": relative_label(root),
        "ecosystems": {key: sorted(set(value)) for key, value in dependencies.items()},
    }


def _toml_dependency_names(path: Path, ctx: ToolContext) -> list[str]:
    text = read_text(path, ctx)
    names: list[str] = []
    in_deps = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("dependencies") and stripped.endswith("["):
            in_deps = True
            continue
        if in_deps:
            if stripped.startswith("]"):
                in_deps = False
                continue
            match = re.match(r'^"([A-Za-z0-9_.\-]+)', stripped)
            if match:
                names.append(match.group(1))
    return names


def _inspect_build(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    root = jail_dir(args, ctx)
    project = detect_project(root)
    artifacts = [
        relative_label(path)
        for path in (root / "dist", root / "build", root / ".next")
        if path.exists()
    ]
    entry_points = [
        relative_label(path)
        for name in ("main.py", "app.py", "index.ts", "index.js", "main.go", "main.rs")
        if (path := root / name).is_file()
    ]
    return {
        "root": relative_label(root),
        "languages": project["languages"],
        "package_manager": (
            project["projects"][0]["package_manager"] if project["detected"] else None
        ),
        "existing_artifacts": artifacts,
        "entry_points": entry_points,
        "build_command": _build_command(root),
    }


def _build_command(root: Path) -> str | None:
    if (root / "package.json").is_file():
        return "npm run build"
    if (root / "Cargo.toml").is_file():
        return "cargo build --release"
    if (root / "go.mod").is_file():
        return "go build ./..."
    if (root / "Makefile").is_file():
        return "make build"
    if (root / "pyproject.toml").is_file():
        return "uv build"
    return None


def _register_verify(registry: ToolRegistry, name: str, description: str, handler: Any) -> None:
    registry.register(
        Tool(
            name=name,
            description=description,
            parameters={
                "type": "object",
                "properties": {
                    "path": _str_param("Project directory (default workspaces)"),
                    "command": _str_param("Explicit command override (default: detected)"),
                },
                "required": [],
                "additionalProperties": False,
            },
            risk="execute",
            handler=handler,
            scope=f"coding:{name}",
            group=GROUP,
        )
    )


def register(registry: ToolRegistry, settings: Any = None) -> None:  # noqa: ARG001
    registry.register(
        Tool(
            name="project_detect",
            description=(
                "Detect languages, package manager and the project's own test/lint commands."
            ),
            parameters={
                "type": "object",
                "properties": {"path": _str_param("Project directory (default workspaces)")},
                "required": [],
                "additionalProperties": False,
            },
            risk="read",
            handler=_project_detect,
            group=GROUP,
        )
    )
    registry.register(
        Tool(
            name="repo_search",
            description="Search source files for a regex, optionally filtered by language.",
            parameters={
                "type": "object",
                "properties": {
                    "query": _str_param("Regular expression"),
                    "path": _str_param("Directory to search (default workspaces)"),
                    "language": _str_param("File extension filter, e.g. py"),
                    "max_results": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            risk="read",
            handler=_repo_search,
            group=GROUP,
        )
    )
    registry.register(
        Tool(
            name="read_source",
            description="Read a source file with line numbers, optionally a line range.",
            parameters={
                "type": "object",
                "properties": {
                    "path": _str_param("Source file"),
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            risk="read",
            handler=_read_source,
            group=GROUP,
        )
    )
    registry.register(
        Tool(
            name="edit_source",
            description="Replace exact text in a source file and return the resulting diff.",
            parameters={
                "type": "object",
                "properties": {
                    "path": _str_param("Source file"),
                    "old": _str_param("Exact text to replace"),
                    "new": _str_param("Replacement text"),
                    "replace_all": {"type": "boolean"},
                },
                "required": ["path", "old", "new"],
                "additionalProperties": False,
            },
            risk="write",
            handler=_edit_source,
            scope="code:write",
            group=GROUP,
        )
    )
    registry.register(
        Tool(
            name="apply_patch",
            description="Apply a unified diff to a source file (all hunks or nothing).",
            parameters={
                "type": "object",
                "properties": {
                    "path": _str_param("Source file"),
                    "patch": _str_param("Unified diff text"),
                },
                "required": ["path", "patch"],
                "additionalProperties": False,
            },
            risk="write",
            handler=_apply_patch,
            scope="code:write",
            group=GROUP,
        )
    )
    _register_verify(registry, "run_tests", "Run the project's test command.", _run_tests)
    _register_verify(registry, "run_linter", "Run the project's lint command.", _run_linter)
    _register_verify(
        registry, "run_typecheck", "Run the project's typecheck command.", _run_typecheck
    )
    _register_verify(registry, "run_formatter", "Run the project's formatter.", _run_formatter)
    registry.register(
        Tool(
            name="inspect_dependencies",
            description=(
                "List declared dependencies from pyproject.toml / package.json / requirements.txt."
            ),
            parameters={
                "type": "object",
                "properties": {"path": _str_param("Project directory")},
                "required": [],
                "additionalProperties": False,
            },
            risk="read",
            handler=_inspect_dependencies,
            group=GROUP,
        )
    )
    registry.register(
        Tool(
            name="inspect_build",
            description=(
                "Report build system, entry points, existing artifacts and the build command."
            ),
            parameters={
                "type": "object",
                "properties": {"path": _str_param("Project directory")},
                "required": [],
                "additionalProperties": False,
            },
            risk="read",
            handler=_inspect_build,
            group=GROUP,
        )
    )


__all__ = [
    "GROUP",
    "MAX_RESULTS",
    "detect_project",
    "jail_dir",
    "register",
]
