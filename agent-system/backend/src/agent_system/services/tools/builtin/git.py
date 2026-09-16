"""Git capabilities (v3.1 §14) — explicit, not "whatever shell allows".

Read operations (``git_status``, ``git_diff``, ``git_log``, ``git_show``,
``git_branch_list``) are ``read`` tier: no approval, no mutation.

Write operations (``git_add``, ``git_commit``, ``git_checkout``,
``git_restore``) are ``write`` tier: gated, and scoped to the repository so an
approval covers one repo, not every repo on the machine.

Destructive operations (``git_reset_hard``, ``git_clean``, ``git_push_force``)
are ``destructive`` tier and therefore default-deny: declaring them here means
the capability exists in the inventory and is auditable, while being
unequivocally refused. An agent cannot reach them by shelling out either —
``shell`` is itself gated and treated as an execute-tier capability.

Every command runs with explicit argv (never a shell string), in the workspace
sandbox, with ``GIT_TERMINAL_PROMPT=0`` so a credential prompt cannot hang a run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_system.services.sandbox import quote_args
from agent_system.services.tool_errors import ToolError
from agent_system.services.tools.builtin._exec import run_workspace_command, workspace_dir
from agent_system.services.tools.registry import Tool, ToolContext, ToolRegistry, _str_param

GROUP = "git"
MAX_GIT_OUTPUT_LINES = 200


def _git(ctx: ToolContext, argv: list[str], cwd: str | None) -> dict[str, Any]:
    workdir = workspace_dir(ctx, cwd)
    command = "GIT_TERMINAL_PROMPT=0 git " + " ".join(quote_args(argv))
    result = run_workspace_command(ctx, command, cwd=str(workdir))
    output = str(result.get("output") or "")
    lines = output.splitlines()
    if len(lines) > MAX_GIT_OUTPUT_LINES:
        output = "\n".join(lines[:MAX_GIT_OUTPUT_LINES]) + "\n…[truncated]"
    return {
        "exit_code": result.get("exit_code"),
        "output": output,
        "mode": result.get("mode"),
        "cwd": str(workdir),
    }


def _repo_arg(args: dict[str, Any]) -> str | None:
    repo = str(args.get("repo") or "").strip()
    return repo or None


# -- read -------------------------------------------------------------------


def _git_status(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return _git(ctx, ["status", "--short", "--branch"], _repo_arg(args))


def _git_diff(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    argv = ["diff"]
    if bool(args.get("staged")):
        argv.append("--cached")
    if args.get("path"):
        argv += ["--", str(args["path"])]
    return _git(ctx, argv, _repo_arg(args))


def _git_log(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    limit = min(int(args.get("limit") or 10), 100)
    argv = ["log", f"-n{limit}", "--pretty=format:%h %ad %an %s", "--date=short"]
    if args.get("path"):
        argv += ["--", str(args["path"])]
    return _git(ctx, argv, _repo_arg(args))


def _git_show(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    revision = str(args.get("revision") or "HEAD")
    if revision.startswith("-"):
        raise ToolError("git_show: revision must not be an option")
    argv = ["show", "--stat", revision]
    if args.get("path"):
        argv += ["--", str(args["path"])]
    return _git(ctx, argv, _repo_arg(args))


def _git_branch_list(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return _git(ctx, ["branch", "-a", "-vv"], _repo_arg(args))


# -- write ------------------------------------------------------------------


def _git_add(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    paths = args.get("paths")
    if not isinstance(paths, list) or not paths:
        raise ToolError("git_add: 'paths' must be a non-empty list of paths")
    for path in paths:
        if str(path).startswith("-"):
            raise ToolError("git_add: paths must not be options")
    return _git(ctx, ["add", "--", *[str(p) for p in paths]], _repo_arg(args))


def _git_commit(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    message = str(args.get("message") or "").strip()
    if not message:
        raise ToolError("git_commit: 'message' is required")
    return _git(ctx, ["commit", "-m", message], _repo_arg(args))


def _git_checkout(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    ref = str(args.get("ref") or "").strip()
    if not ref or ref.startswith("-"):
        raise ToolError("git_checkout: 'ref' is required and must not be an option")
    argv = ["checkout", ref]
    if bool(args.get("create_branch")):
        argv = ["checkout", "-b", ref]
    return _git(ctx, argv, _repo_arg(args))


def _write_scope(args: dict[str, Any]) -> str:
    """Bind a write approval to the repository it applies to, not to every repo."""
    repo = str(args.get("repo") or ".")
    try:
        label = str(Path(repo).resolve().relative_to(Path.cwd()))
    except (OSError, ValueError):
        label = repo
    return f"git:write:{label}"


def _git_restore(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    paths = args.get("paths")
    if not isinstance(paths, list) or not paths:
        raise ToolError("git_restore: 'paths' must be a non-empty list of paths")
    for path in paths:
        if str(path).startswith("-"):
            raise ToolError("git_restore: paths must not be options")
    return _git(ctx, ["restore", "--", *[str(p) for p in paths]], _repo_arg(args))


# -- destructive (declared, and default-deny) --------------------------------


def _git_reset_hard(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    revision = str(args.get("revision") or "HEAD")
    if revision.startswith("-"):
        raise ToolError("git_reset_hard: revision must not be an option")
    return _git(ctx, ["reset", "--hard", revision], _repo_arg(args))


def _git_clean(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return _git(ctx, ["clean", "-fdx"], _repo_arg(args))


def _git_push_force(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    remote = str(args.get("remote") or "origin")
    branch = str(args.get("branch") or "HEAD")
    for value in (remote, branch):
        if value.startswith("-"):
            raise ToolError("git_push_force: remote/branch must not be options")
    return _git(ctx, ["push", "--force", remote, branch], _repo_arg(args))


_READ_TOOLS = (
    ("git_status", "Show working-tree status (short) with the current branch.", _git_status),
    ("git_diff", "Show the working-tree or staged diff.", _git_diff),
    ("git_log", "Show recent commits (hash, date, author, subject).", _git_log),
    ("git_show", "Show a revision with its diffstat.", _git_show),
    ("git_branch_list", "List local and remote branches.", _git_branch_list),
)

_WRITE_TOOLS = (
    ("git_add", "Stage paths (explicit paths, never `-A`).", _git_add),
    ("git_commit", "Commit staged changes with a message.", _git_commit),
    ("git_checkout", "Check out a ref, optionally creating a branch.", _git_checkout),
    ("git_restore", "Restore paths from HEAD (discards uncommitted edits to them).", _git_restore),
)

_DESTRUCTIVE_TOOLS = (
    (
        "git_reset_hard",
        "Reset the tree to a revision, discarding all local changes.",
        _git_reset_hard,
    ),
    ("git_clean", "Delete untracked files and directories.", _git_clean),
    ("git_push_force", "Force-push a branch, rewriting remote history.", _git_push_force),
)


def _paths_schema() -> dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": "string"},
        "minItems": 1,
    }


def register(registry: ToolRegistry, settings: Any = None) -> None:  # noqa: ARG001
    repo = {"type": "string", "description": "Repository directory (default workspaces)"}
    common = {"repo": repo, "path": _str_param("Optional path filter")}
    for name, description, handler in _READ_TOOLS:
        parameters: dict[str, Any] = {
            "type": "object",
            "properties": dict(common),
            "required": [],
            "additionalProperties": False,
        }
        if name == "git_diff":
            parameters["properties"]["staged"] = {"type": "boolean"}
        if name == "git_log":
            parameters["properties"]["limit"] = {"type": "integer", "minimum": 1, "maximum": 100}
        if name == "git_show":
            parameters["properties"]["revision"] = _str_param("Revision (default HEAD)")
        registry.register(
            Tool(
                name=name,
                description=description,
                parameters=parameters,
                risk="read",
                handler=handler,
                group=GROUP,
            )
        )

    for name, description, handler in _WRITE_TOOLS:
        if name in {"git_add", "git_restore"}:
            parameters = {
                "type": "object",
                "properties": {"repo": repo, "paths": _paths_schema()},
                "required": ["paths"],
                "additionalProperties": False,
            }
        elif name == "git_commit":
            parameters = {
                "type": "object",
                "properties": {"repo": repo, "message": _str_param("Commit message")},
                "required": ["message"],
                "additionalProperties": False,
            }
        else:
            parameters = {
                "type": "object",
                "properties": {
                    "repo": repo,
                    "ref": _str_param("Branch, tag or commit"),
                    "create_branch": {"type": "boolean"},
                },
                "required": ["ref"],
                "additionalProperties": False,
            }
        registry.register(
            Tool(
                name=name,
                description=description,
                parameters=parameters,
                risk="write",
                handler=handler,
                scope=_write_scope,
                group=GROUP,
            )
        )

    for name, description, handler in _DESTRUCTIVE_TOOLS:
        parameters = {
            "type": "object",
            "properties": {"repo": repo},
            "required": [],
            "additionalProperties": False,
        }
        if name == "git_reset_hard":
            parameters["properties"]["revision"] = _str_param("Revision (default HEAD)")
        if name == "git_push_force":
            parameters["properties"]["remote"] = _str_param("Remote (default origin)")
            parameters["properties"]["branch"] = _str_param("Branch (default HEAD)")
        registry.register(
            Tool(
                name=name,
                description=description,
                parameters=parameters,
                risk="destructive",
                handler=handler,
                scope=f"host:git:{name}",
                destructive_reason=(
                    "destructive git operations are default-deny: they can rewrite or "
                    "delete work that is not recoverable from the repository"
                ),
                group=GROUP,
            )
        )


__all__ = ["GROUP", "register"]
