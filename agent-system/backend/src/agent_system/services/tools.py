"""Agent tools — OpenClaw-style Hands for the ReAct loop.

A Tool is a typed function the model can call (shell, files, web, memory,
task inspection, OpenConnector SaaS actions, MCP servers). Tools are exposed
to the model as JSON schemas and executed by ``services/agent_loop.py``,
which works uniformly across all providers via fenced
`````tool:name`` blocks — no provider-native function calling required.

Safety model (mirrors OpenClaw tool policy):
- every tool declares a risk tier: ``read`` | ``write`` | ``execute``.
- ``execute`` tools (shell, openconnector_execute, mcp_call, execute-risk
  plugins) run gated: shell runs in the Docker sandbox by default
  (``tools_shell_mode=sandbox``); ``local`` mode runs on the host and is
  opt-in via settings; ``off`` disables shell entirely.
- when ``tools_require_approval`` is set, execute tools demand a live
  approval: without one they persist an ``approval.requested`` row and raise
  ``NeedsApprovalError`` — the task fails honestly with the approval id, the
  user approves (`/approve`, dashboard, Telegram), and retries the task.
- file tools are jailed to allowed roots and never touch secret paths.
- every call emits ``tool.called`` / ``tool.result`` (audit trail in the DB).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from agent_system.services.tool_plugins import (
    ToolPlugin,
    ToolPluginError,
    ToolPluginManager,
)

RISK_READ = "read"
RISK_WRITE = "write"
RISK_EXECUTE = "execute"


class ToolError(RuntimeError):
    """A tool ran and failed (reported back to the model)."""


class NeedsApprovalError(ToolError):
    """Raised when an execute tool needs an approval first.

    Carries the persisted approval id so the runner surfaces it and the
    user can approve + retry.
    """

    def __init__(self, approval_id: str, action: str) -> None:
        super().__init__(f"awaiting approval {approval_id} for: {action}")
        self.approval_id = approval_id
        self.action = action


@dataclass
class ToolContext:
    """Everything a tool handler may need (no globals)."""

    settings: Any
    factory: Any = None
    session_id: str | None = None
    task_id: str | None = None
    agent_run_id: str | None = None
    agent_type: str | None = None
    emit: Callable[[str, dict[str, Any]], None] | None = None


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    risk: str
    handler: Callable[[dict[str, Any], ToolContext], dict[str, Any]]

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class ToolRegistry:
    """Name -> Tool, plus schema rendering for prompts."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [self._tools[name].schema() for name in self.names()]

    def prompt_block(self) -> str:
        """Compact tool catalog rendered into the ReAct system prompt."""
        lines = [
            "You have these tools. To call one, emit a fenced block:",
            "```tool:<name>",
            '{"arg": "value"}',
            "```",
            "One call per block; you may emit several blocks per turn. "
            "Results come back as <tool_result> blocks — then continue reasoning.",
            "",
        ]
        for name in self.names():
            tool = self._tools[name]
            params = tool.parameters.get("properties", {})
            required = tool.parameters.get("required", [])
            sig = ", ".join(f"{key}{'' if key in required else '?'}" for key in params)
            lines.append(f"- {name}({sig}): {tool.description}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers: roots, secrets, approvals
# ---------------------------------------------------------------------------


def _allowed_roots(settings: Any) -> list[Path]:
    roots = [Path.cwd() / "workspaces", Path.cwd() / "outputs", Path.cwd()]
    extra = str(getattr(settings, "tools_fs_roots", "") or "")
    for part in extra.split(","):
        part = part.strip()
        if part:
            roots.append(Path(part).expanduser())
    seen: list[Path] = []
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved not in seen:
            seen.append(resolved)
    return seen


def _jailed(path_str: str, settings: Any) -> Path:
    """Resolve a user path, enforcing jail roots + secret exclusion."""
    from agent_system.services.secrets import is_secret_path

    candidate = Path(path_str).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise ToolError(f"unresolvable path: {path_str}") from exc
    if is_secret_path(resolved.name) or is_secret_path(str(resolved)):
        raise ToolError(f"refusing secret path: {path_str}")
    for root in _allowed_roots(settings):
        try:
            if resolved == root or resolved.is_relative_to(root):
                return resolved
        except (OSError, ValueError):
            continue
    raise ToolError(f"path outside allowed roots: {path_str}")


def _require_execute_approval(ctx: ToolContext, scope: str, action: str) -> None:
    """Gate an execute-tier tool behind a live approval (DB-backed).

    When ``tools_require_approval`` is set, the call needs a live APPROVED
    row for this exact ``scope`` + ``action``; otherwise persist a PENDING
    approval row and raise ``NeedsApprovalError`` carrying its id.
    """
    if not bool(getattr(ctx.settings, "tools_require_approval", True)):
        return
    if _db_approval_ok(ctx.factory, scope, action):
        return
    approval_id = _db_request_approval(ctx.factory, ctx, action, scope, risk="HIGH")
    raise NeedsApprovalError(approval_id, action)


def _db_approval_ok(factory: Any, scope: str, action: str) -> bool:
    """True when a live APPROVED decision covers this scope (DB-backed)."""
    if factory is None:
        return False
    from agent_system.domain.events import utcnow
    from agent_system.infra.db import session_scope
    from agent_system.infra.models import Approval as ApprovalRow

    now = utcnow()
    with session_scope(factory) as db:
        rows = (
            db.query(ApprovalRow)
            .filter_by(scope=scope, decision="APPROVED")
            .order_by(ApprovalRow.created_at.desc())
            .all()
        )
        for row in rows:
            if row.expires_at is not None and row.expires_at <= now:
                continue
            if row.requested_action and row.requested_action != action:
                # ALLOW_ONCE-equivalent: exact-action match required.
                continue
            return True
    return False


def _db_request_approval(
    factory: Any,
    ctx: ToolContext,
    action: str,
    scope: str,
    risk: str,
    minutes: int = 30,
) -> str:
    """Persist a PENDING approval row + event; return its id."""
    from agent_system.domain import ids
    from agent_system.domain.events import utcnow
    from agent_system.infra.db import session_scope
    from agent_system.infra.models import Approval as ApprovalRow

    approval_id = ids.new_approval_id()
    expires = utcnow() + timedelta(minutes=minutes)
    if factory is not None:
        with session_scope(factory) as db:
            db.add(
                ApprovalRow(
                    id=approval_id,
                    task_id=ctx.task_id,
                    agent_run_id=ctx.agent_run_id,
                    requested_action=action,
                    risk=risk,
                    scope=scope,
                    requester=ctx.agent_type or "agent",
                    decision="PENDING",
                    expires_at=expires,
                )
            )
            if ctx.emit is not None:
                ctx.emit(
                    "approval.requested",
                    {
                        "approval_id": approval_id,
                        "scope": scope,
                        "risk": risk,
                        "requested_action": action[:200],
                    },
                )
    elif ctx.emit is not None:
        ctx.emit(
            "approval.requested",
            {"approval_id": approval_id, "scope": scope, "risk": risk},
        )
    return approval_id


def _scrub(text: str) -> str:
    try:
        from agent_system.services.memory import scrub_text

        return scrub_text(text)
    except Exception:
        return text


# ---------------------------------------------------------------------------
# Builtin tool handlers
# ---------------------------------------------------------------------------


def _shell(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    settings = ctx.settings
    mode = str(getattr(settings, "tools_shell_mode", "sandbox") or "sandbox")
    command = str(args.get("command") or "").strip()
    if not command:
        raise ToolError("shell: 'command' is required")
    if mode == "off":
        raise ToolError("shell tool is disabled (tools_shell_mode=off)")
    cwd = str(args.get("cwd") or "workspaces")
    raw_timeout = args.get("timeout")
    timeout = int(
        raw_timeout
        if raw_timeout is not None
        else getattr(settings, "max_execution_time_seconds", 300)
    )
    scope = f"shell:{cwd}"
    _require_execute_approval(ctx, scope, command)
    if bool(getattr(settings, "heroku_jail", False)):
        return _jail_shell(settings, command, cwd, timeout)
    if mode == "sandbox":
        try:
            from agent_system.services.sandbox import DockerSandbox
        except Exception as exc:
            raise ToolError(f"sandbox unavailable: {exc}") from exc
        ws = Path(cwd)
        if not ws.is_absolute():
            ws = Path.cwd() / ws
        ws.mkdir(parents=True, exist_ok=True)
        try:
            sandbox = DockerSandbox()
            outcome = sandbox.run(str(ws), command, timeout_seconds=timeout)
        except Exception as exc:
            raise ToolError(f"sandbox exec failed: {exc}") from exc
        return {
            "exit_code": outcome.get("exit_code"),
            "output": _scrub(str(outcome.get("stdout", ""))[-4000:]),
        }
    # mode == local (explicit opt-in): run on the host.
    if mode != "local":
        raise ToolError(f"unknown tools_shell_mode '{mode}' (sandbox|local|off)")
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(Path.cwd() / cwd) if not Path(cwd).is_absolute() else cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"command timed out after {timeout}s") from exc
    output = (proc.stdout or "") + (proc.stderr or "")
    return {"exit_code": proc.returncode, "output": _scrub(output[-4000:])}


def _jail_shell(settings: Any, command: str, cwd: str, timeout: int) -> dict[str, Any]:
    """Execute a shell command in the cloud subprocess jail (no Docker).

    cwd is resolved under the jail root; secrets are stripped from the
    child env; rlimits + timeout + output caps enforced (see
    services/sandbox.py). Containment, not isolation — the approval gate
    (checked by the caller) remains the primary control.
    """
    from agent_system.services.sandbox import SandboxError, SubprocessJail

    allowlist = str(getattr(settings, "heroku_shell_allowlist", "") or "")
    ws = Path(cwd)
    if not ws.is_absolute():
        ws = Path.cwd() / ws
    try:
        outcome = SubprocessJail().run(
            str(ws), command, timeout_seconds=timeout, allowlist=allowlist
        )
    except SandboxError as exc:
        raise ToolError(f"jail exec failed: {exc}") from exc
    except Exception as exc:
        raise ToolError(f"jail exec failed: {exc}") from exc
    return {
        "exit_code": outcome.get("exit_code"),
        "output": _scrub(str(outcome.get("stdout", ""))[-4000:]),
    }


def _file_read(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    target = _jailed(str(args.get("path") or ""), ctx.settings)
    max_bytes = int(getattr(ctx.settings, "max_file_size_mb", 10)) * 1024 * 1024
    try:
        data = target.read_bytes()[:max_bytes]
    except FileNotFoundError as exc:
        raise ToolError(f"file not found: {args.get('path')}") from exc
    except OSError as exc:
        raise ToolError(f"cannot read file: {exc}") from exc
    try:
        return {"path": str(target), "content": data.decode("utf-8")}
    except UnicodeDecodeError:
        return {"path": str(target), "content": data.hex(), "encoding": "hex"}


def _file_write(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    target = _jailed(str(args.get("path") or ""), ctx.settings)
    content = str(args.get("content") or "")
    max_bytes = int(getattr(ctx.settings, "max_file_size_mb", 10)) * 1024 * 1024
    if len(content.encode("utf-8")) > max_bytes:
        raise ToolError(f"content exceeds max_file_size_mb ({len(content)} chars)")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"path": str(target), "bytes": len(content.encode("utf-8"))}


def _file_list(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    target = _jailed(str(args.get("path") or "."), ctx.settings)
    if not target.is_dir():
        raise ToolError(f"not a directory: {args.get('path')}")
    entries = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
    return {"path": str(target), "entries": entries[:200]}


def _web_fetch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    url = str(args.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ToolError("web_fetch: http(s) URL required")
    try:
        from agent_system.agents.browser_research import ResearchAgent
    except Exception as exc:
        raise ToolError(f"research backend unavailable: {exc}") from exc
    try:
        result = ResearchAgent().research([url])
    except Exception as exc:
        raise ToolError(f"fetch failed: {exc}") from exc
    citations = result.get("citations", []) if isinstance(result, dict) else []
    errors = result.get("errors", {}) if isinstance(result, dict) else {}
    if errors:
        raise ToolError(f"fetch failed: {list(errors.values())[0]}")
    if not citations:
        raise ToolError("no content extracted")
    cite = citations[0] if isinstance(citations[0], dict) else {}
    text = str(cite.get("snippet", ""))
    return {
        "url": url,
        "title": str(cite.get("title", "")),
        "text": text[:8000],
    }


def _memory_recall(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = str(args.get("query") or "")
    top_k = int(args.get("top_k") or 3)
    try:
        from agent_system.services.memory_hooks import recall_recent
    except Exception as exc:
        raise ToolError(f"memory unavailable: {exc}") from exc
    notes = recall_recent(ctx.settings, query=query, limit=top_k, factory=ctx.factory)
    return {"memories": notes}


def _memory_remember(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    fact = str(args.get("fact") or "").strip()
    if not fact:
        raise ToolError("memory_remember: 'fact' is required")
    try:
        from agent_system.services.memory_hooks import remember_fact
    except Exception as exc:
        raise ToolError(f"memory unavailable: {exc}") from exc
    record_id = remember_fact(
        ctx.settings,
        fact,
        session_id=ctx.session_id,
        task_id=ctx.task_id,
        tags=["agent-tool"],
        factory=ctx.factory,
    )
    return {"memory_id": record_id}


def _tasks_inspect(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if ctx.factory is None:
        raise ToolError("task inspection needs a session factory")
    from agent_system.infra.db import session_scope
    from agent_system.infra.models import Task as TaskRow

    session_id = str(args.get("session_id") or ctx.session_id or "")
    with session_scope(ctx.factory) as db:
        query = db.query(TaskRow)
        if session_id:
            query = query.filter_by(session_id=session_id)
        rows = query.order_by(TaskRow.created_at.desc()).limit(20).all()
        return {
            "tasks": [
                {"id": r.id, "title": r.title, "state": r.state, "agent_type": r.agent_type}
                for r in rows
            ]
        }


def _openconnector_execute(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        from agent_system.services.openconnector import execute_action, is_configured
    except Exception as exc:
        raise ToolError(f"openconnector unavailable: {exc}") from exc
    if not is_configured(ctx.settings):
        raise ToolError("openconnector not configured (set OPENCONNECTOR_BASE_URL)")
    # 'action' is canonical; 'slug' stays accepted for older transcripts.
    action = str(args.get("action") or args.get("slug") or "").strip()
    if not action:
        raise ToolError(
            "openconnector_execute: 'action' is required (e.g. github.get_current_user)"
        )
    # 'input' is canonical; 'arguments' stays accepted for older transcripts.
    input_data = args.get("input", args.get("arguments"))
    if input_data is not None and not isinstance(input_data, dict):
        raise ToolError("'input' must be an object")
    connection = args.get("connection")
    if connection is not None and not isinstance(connection, str):
        raise ToolError("'connection' must be a string (connection name)")
    _require_execute_approval(ctx, f"openconnector:{action}", action)
    try:
        return execute_action(ctx.settings, action, input_data or {}, connection)
    except Exception as exc:
        raise ToolError(f"openconnector call failed: {exc}") from exc


def _openconnector_list(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Discover OpenConnector actions (optionally per service / by query)."""
    try:
        from agent_system.services.openconnector import is_configured, list_actions
    except Exception as exc:
        raise ToolError(f"openconnector unavailable: {exc}") from exc
    if not is_configured(ctx.settings):
        raise ToolError("openconnector not configured (set OPENCONNECTOR_BASE_URL)")
    service = str(args.get("service") or "").strip() or None
    query = str(args.get("query") or "").strip().lower()
    try:
        actions = list_actions(ctx.settings, service)
    except Exception as exc:
        raise ToolError(f"openconnector list failed: {exc}") from exc
    if query:
        actions = [
            a
            for a in actions
            if query in str(a.get("id", "")).lower()
            or query in str(a.get("name", "")).lower()
            or query in str(a.get("description", "")).lower()
        ]
    compact = [
        {
            "id": a.get("id"),
            "name": a.get("name"),
            "description": str(a.get("description", ""))[:160],
            "service": a.get("service") or str(a.get("id", "")).split(".")[0],
        }
        for a in actions[:50]
    ]
    return {"count": len(actions), "shown": len(compact), "actions": compact}


def _mcp_list(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """List tools on a configured MCP server (stdio or HTTP/OpenConnector)."""
    try:
        from agent_system.services.mcp import all_servers, list_server_tools
    except Exception as exc:
        raise ToolError(f"mcp unavailable: {exc}") from exc
    server = str(args.get("server") or "").strip()
    if not server:
        names = [s.name for s in all_servers(ctx.settings)]
        return {"servers": names, "hint": "pass 'server' to list its tools"}
    try:
        tools = list_server_tools(ctx.settings, server)
    except Exception as exc:
        raise ToolError(f"mcp list failed: {exc}") from exc
    compact = [
        {
            "name": t.get("name"),
            "description": str(t.get("description", ""))[:160],
        }
        for t in tools[:50]
    ]
    return {"server": server, "count": len(tools), "shown": len(compact), "tools": compact}


def _mcp_call(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        from agent_system.services.mcp import call_tool as mcp_call_tool
        from agent_system.services.mcp import is_configured as mcp_configured
    except Exception as exc:
        raise ToolError(f"mcp unavailable: {exc}") from exc
    if not mcp_configured(ctx.settings):
        raise ToolError("no MCP servers configured (set MCP_SERVERS JSON)")
    server = str(args.get("server") or "")
    tool = str(args.get("tool") or "")
    arguments = args.get("arguments") or {}
    if not server or not tool:
        raise ToolError("mcp_call needs 'server' and 'tool'")
    if not isinstance(arguments, dict):
        raise ToolError("'arguments' must be an object")
    _require_execute_approval(ctx, f"mcp:{server}:{tool}", f"{server}.{tool}")
    try:
        return mcp_call_tool(ctx.settings, server, tool, arguments)
    except Exception as exc:
        raise ToolError(f"mcp call failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Registry assembly
# ---------------------------------------------------------------------------


def _str_param(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def build_registry(settings: Any, plugin_dir: Path | str | None = None) -> ToolRegistry:
    """Assemble the tool registry for these settings (OC/MCP added if configured).

    Enabled tool plugins from ``plugin_dir`` (default
    ``settings.tools_plugin_dir``) are merged last. Discovery re-reads the
    plugin dir on every call, so enabling/disabling a plugin takes effect on
    the next task with no restart. Plugins can never shadow built-in tools.
    """
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="shell",
            description=(
                "Run a shell command (sandboxed unless tools_shell_mode=local). "
                "May require approval."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": _str_param("Shell command to run"),
                    "cwd": {
                        "type": "string",
                        "description": "Working directory (default workspaces)",
                    },
                    "timeout": {"type": "integer", "description": "Timeout seconds"},
                },
                "required": ["command"],
            },
            risk=RISK_EXECUTE,
            handler=_shell,
        )
    )
    registry.register(
        Tool(
            name="file_read",
            description="Read a UTF-8 text file under allowed roots.",
            parameters={
                "type": "object",
                "properties": {"path": _str_param("File path to read")},
                "required": ["path"],
            },
            risk=RISK_READ,
            handler=_file_read,
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
                    "content": _str_param("File content"),
                },
                "required": ["path", "content"],
            },
            risk=RISK_WRITE,
            handler=_file_write,
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
            },
            risk=RISK_READ,
            handler=_file_list,
        )
    )
    registry.register(
        Tool(
            name="web_fetch",
            description="Fetch a URL and return title + readable text.",
            parameters={
                "type": "object",
                "properties": {"url": _str_param("http(s) URL")},
                "required": ["url"],
            },
            risk=RISK_READ,
            handler=_web_fetch,
        )
    )
    registry.register(
        Tool(
            name="memory_recall",
            description="Recall recent vault memories relevant to a query.",
            parameters={
                "type": "object",
                "properties": {
                    "query": _str_param("What to look for"),
                    "top_k": {"type": "integer", "description": "Max notes (default 3)"},
                },
                "required": ["query"],
            },
            risk=RISK_READ,
            handler=_memory_recall,
        )
    )
    registry.register(
        Tool(
            name="memory_remember",
            description="Store a durable fact in the Obsidian vault.",
            parameters={
                "type": "object",
                "properties": {"fact": _str_param("Fact to remember")},
                "required": ["fact"],
            },
            risk=RISK_WRITE,
            handler=_memory_remember,
        )
    )
    registry.register(
        Tool(
            name="tasks_inspect",
            description="List recent tasks (optionally for a session).",
            parameters={
                "type": "object",
                "properties": {"session_id": _str_param("Session id")},
                "required": [],
            },
            risk=RISK_READ,
            handler=_tasks_inspect,
        )
    )
    try:
        from agent_system.services.openconnector import is_configured as _oc_ok

        if _oc_ok(settings):
            registry.register(
                Tool(
                    name="openconnector_execute",
                    description=(
                        "Run a SaaS Action via OpenConnector, e.g. "
                        "github.get_current_user. Credentials live in the "
                        "OpenConnector runtime, not here."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "action": _str_param("Action id like github.get_current_user"),
                            "input": {
                                "type": "object",
                                "description": "Action input object",
                            },
                            "connection": _str_param("Optional named connection (else default)"),
                        },
                        "required": ["action"],
                    },
                    risk=RISK_EXECUTE,
                    handler=_openconnector_execute,
                )
            )
            registry.register(
                Tool(
                    name="openconnector_list",
                    description=(
                        "List/search available OpenConnector actions "
                        "(optionally per service) before executing one."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "service": _str_param("Filter by provider service (e.g. github)"),
                            "query": _str_param("Search text in id/name/description"),
                        },
                        "required": [],
                    },
                    risk=RISK_READ,
                    handler=_openconnector_list,
                )
            )
    except Exception:
        pass
    try:
        from agent_system.services.mcp import is_configured as _mcp_ok

        if _mcp_ok(settings):
            registry.register(
                Tool(
                    name="mcp_list",
                    description=(
                        "List tools exposed by a configured MCP server "
                        "(stdio servers or the OpenConnector HTTP gateway)."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "server": _str_param("Server name (omit to list server names)"),
                        },
                        "required": [],
                    },
                    risk=RISK_READ,
                    handler=_mcp_list,
                )
            )
            registry.register(
                Tool(
                    name="mcp_call",
                    description="Call a tool on a configured MCP server.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "server": _str_param("MCP server name"),
                            "tool": _str_param("Tool name on that server"),
                            "arguments": {"type": "object", "description": "Tool arguments"},
                        },
                        "required": ["server", "tool"],
                    },
                    risk=RISK_EXECUTE,
                    handler=_mcp_call,
                )
            )
    except Exception:
        pass
    manager = ToolPluginManager(
        plugin_dir
        if plugin_dir is not None
        else getattr(settings, "tools_plugin_dir", "tools_plugins")
    )
    for plugin_tool in manager.build_tools():
        if registry.get(plugin_tool.name) is not None:
            manager.last_errors.append(f"{plugin_tool.name}: refusing to shadow a built-in tool")
            continue
        registry.register(plugin_tool)
    return registry


__all__ = [
    "NeedsApprovalError",
    "RISK_EXECUTE",
    "RISK_READ",
    "RISK_WRITE",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolPluginManager",
    "ToolPlugin",
    "ToolPluginError",
    "ToolRegistry",
    "build_registry",
]
