"""Optional integration capabilities (MCP + OpenConnector).

These capabilities exist only when the deployment configures the matching
integration: no ``OPENCONNECTOR_BASE_URL`` means the OpenConnector capabilities
are absent from the registry entirely (rather than present and always failing).
That keeps the prompt catalog honest about what the agent can actually do.

Both are execute-tier: a remote action can create, send or delete things in a
third-party system, so each call needs a live approval scoped to the specific
action (``openconnector:<action>``, ``mcp:<server>:<tool>``). The remote
credentials live in the connector runtime, never in this process.
"""

from __future__ import annotations

from typing import Any

from agent_system.services.tool_errors import ToolError
from agent_system.services.tools.registry import Tool, _str_param


def _openconnector_execute(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    from agent_system.services.openconnector import execute_action, is_configured

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
    try:
        return execute_action(ctx.settings, action, input_data or {}, connection)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"openconnector call failed: {exc}") from exc


def _openconnector_list(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    from agent_system.services.openconnector import is_configured, list_actions

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
            action
            for action in actions
            if query in str(action.get("id", "")).lower()
            or query in str(action.get("name", "")).lower()
            or query in str(action.get("description", "")).lower()
        ]
    compact = [
        {
            "id": action.get("id"),
            "name": action.get("name"),
            "description": str(action.get("description", ""))[:160],
            "service": action.get("service") or str(action.get("id", "")).split(".")[0],
        }
        for action in actions[:50]
    ]
    return {"count": len(actions), "shown": len(compact), "actions": compact}


def _mcp_list(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    from agent_system.services.mcp import all_servers, list_server_tools

    server = str(args.get("server") or "").strip()
    if not server:
        names = [s.name for s in all_servers(ctx.settings)]
        return {"servers": names, "hint": "pass 'server' to list its tools"}
    try:
        tools = list_server_tools(ctx.settings, server)
    except Exception as exc:
        raise ToolError(f"mcp list failed: {exc}") from exc
    compact = [
        {"name": tool.get("name"), "description": str(tool.get("description", ""))[:160]}
        for tool in tools[:50]
    ]
    return {"server": server, "count": len(tools), "shown": len(compact), "tools": compact}


def _mcp_call(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    from agent_system.services.mcp import call_tool as mcp_call_tool
    from agent_system.services.mcp import is_configured as mcp_configured

    if not mcp_configured(ctx.settings):
        raise ToolError("no MCP servers configured (set MCP_SERVERS JSON)")
    server = str(args.get("server") or "")
    tool = str(args.get("tool") or "")
    arguments = args.get("arguments") or {}
    if not server or not tool:
        raise ToolError("mcp_call needs 'server' and 'tool'")
    if not isinstance(arguments, dict):
        raise ToolError("'arguments' must be an object")
    try:
        return mcp_call_tool(ctx.settings, server, tool, arguments)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"mcp call failed: {exc}") from exc


def _oc_scope(args: dict[str, Any]) -> str:
    action = str(args.get("action") or args.get("slug") or "unknown")
    return f"openconnector:{action}"


def _mcp_scope(args: dict[str, Any]) -> str:
    return f"mcp:{args.get('server')}:{args.get('tool')}"


def register_optional(settings: Any) -> list[Tool]:
    """Capabilities for whichever integrations are configured (may be empty)."""
    tools: list[Tool] = []
    try:
        from agent_system.services.openconnector import is_configured as oc_configured

        if oc_configured(settings):
            tools.append(
                Tool(
                    name="openconnector_execute",
                    description=(
                        "Run a SaaS Action via OpenConnector, e.g. github.get_current_user. "
                        "Credentials live in the OpenConnector runtime, not here."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "action": _str_param("Action id like github.get_current_user"),
                            "input": {"type": "object", "description": "Action input object"},
                            "connection": _str_param("Optional named connection (else default)"),
                        },
                        "required": ["action"],
                        "additionalProperties": False,
                    },
                    risk="execute",
                    handler=_openconnector_execute,
                    scope=_oc_scope,
                    group="openconnector",
                    kind="openconnector",
                )
            )
            tools.append(
                Tool(
                    name="openconnector_list",
                    description=(
                        "List/search available OpenConnector actions (optionally per service) "
                        "before executing one."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "service": _str_param("Filter by provider service (e.g. github)"),
                            "query": _str_param("Search text in id/name/description"),
                        },
                        "required": [],
                        "additionalProperties": False,
                    },
                    risk="read",
                    handler=_openconnector_list,
                    group="openconnector",
                    kind="openconnector",
                )
            )
    except Exception:
        pass
    try:
        from agent_system.services.mcp import is_configured as mcp_configured

        if mcp_configured(settings):
            tools.append(
                Tool(
                    name="mcp_list",
                    description=(
                        "List tools exposed by a configured MCP server "
                        "(stdio servers or the OpenConnector HTTP gateway)."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {"server": _str_param("Server name (omit to list names)")},
                        "required": [],
                        "additionalProperties": False,
                    },
                    risk="read",
                    handler=_mcp_list,
                    group="mcp",
                    kind="mcp",
                )
            )
            tools.append(
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
                        "additionalProperties": False,
                    },
                    risk="execute",
                    handler=_mcp_call,
                    scope=_mcp_scope,
                    group="mcp",
                    kind="mcp",
                )
            )
    except Exception:
        pass
    return tools


__all__ = ["register_optional"]
