"""System capabilities — introspection of the running system.

``capabilities_list`` answers "what can I do, and what would it cost me in
permissions?" straight from the registry, so an agent never has to guess from
its prompt. ``system_status`` reports counts derived from the canonical store
rather than from process-local counters.
"""

from __future__ import annotations

from typing import Any

from agent_system.services.tools.registry import Tool, ToolContext, ToolRegistry, _str_param

GROUP = "system"


def _capabilities_list(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from agent_system.services.tools.registry import build_registry

    registry = build_registry(ctx.settings)
    group = str(args.get("group") or "").strip()
    tools = registry.by_group(group) if group else registry.tools()
    return {
        "count": len(tools),
        "groups": sorted({tool.group for tool in registry.tools()}),
        "capabilities": [tool.to_json() for tool in tools],
    }


def _system_status(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:  # noqa: ARG001
    if ctx.factory is None:
        return {"database": "unavailable (no session factory in this context)"}
    from sqlalchemy import func, select

    from agent_system.infra.db import session_scope
    from agent_system.infra.models import EventRow, ModelCall, Task

    with session_scope(ctx.factory) as db:
        counts = {
            "tasks": db.execute(select(func.count()).select_from(Task)).scalar_one(),
            "events": db.execute(select(func.count()).select_from(EventRow)).scalar_one(),
            "model_calls": db.execute(select(func.count()).select_from(ModelCall)).scalar_one(),
        }
        spend = db.execute(select(func.coalesce(func.sum(ModelCall.cost_usd), 0.0))).scalar_one()
    return {
        "counts": counts,
        "model_spend_usd": round(float(spend or 0.0), 6),
        "settings": {
            "environment": getattr(ctx.settings, "agent_env", None),
            "shell_mode": getattr(ctx.settings, "tools_shell_mode", None),
            "approvals_required": bool(getattr(ctx.settings, "tools_require_approval", True)),
            "default_provider": getattr(ctx.settings, "default_provider", None),
        },
    }


def register(registry: ToolRegistry, settings: Any = None) -> None:  # noqa: ARG001
    registry.register(
        Tool(
            name="capabilities_list",
            description="List every available capability with its risk and permission behaviour.",
            parameters={
                "type": "object",
                "properties": {"group": _str_param("Filter by capability group")},
                "required": [],
                "additionalProperties": False,
            },
            risk="read",
            handler=_capabilities_list,
            group=GROUP,
        )
    )
    registry.register(
        Tool(
            name="system_status",
            description="Report task/event/model-call counts and spend from the canonical store.",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            risk="read",
            handler=_system_status,
            group=GROUP,
        )
    )


__all__ = ["GROUP", "register"]
