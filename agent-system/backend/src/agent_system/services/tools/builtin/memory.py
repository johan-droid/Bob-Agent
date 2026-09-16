"""Memory capabilities (v3.1 §13).

The canonical distinction is preserved exactly:

- **Skill**  = procedural knowledge (``skills/<name>/SKILL.md``, injected into
  the prompt by ``services/skills.py``).
- **Tool**   = executable capability (this package).
- **Memory** = persistent information (``services/memory.py``, the vault).

Memory is therefore *not* a tool: these two capabilities read and write the
memory store, and nothing here makes a memory executable or a skill callable.
"""

from __future__ import annotations

from typing import Any

from agent_system.services.tool_errors import ToolError
from agent_system.services.tools.registry import Tool, ToolContext, ToolRegistry, _str_param

GROUP = "memory"


def _memory_recall(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("memory_recall: 'query' is required")
    try:
        from agent_system.services.memory_hooks import recall_recent
    except Exception as exc:
        raise ToolError(f"memory unavailable: {exc}") from exc
    top_k = int(args.get("top_k") or 3)
    notes = recall_recent(ctx.settings, query=query, limit=top_k, factory=ctx.factory)
    return {"query": query, "count": len(notes), "memories": notes}


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
    return {"memory_id": record_id, "stored": True}


def register(registry: ToolRegistry, settings: Any = None) -> None:  # noqa: ARG001
    registry.register(
        Tool(
            name="memory_recall",
            description="Recall persisted memories relevant to a query.",
            parameters={
                "type": "object",
                "properties": {
                    "query": _str_param("What to look for"),
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            risk="read",
            handler=_memory_recall,
            group=GROUP,
        )
    )
    registry.register(
        Tool(
            name="memory_remember",
            description="Persist a durable fact to memory (never executable).",
            parameters={
                "type": "object",
                "properties": {"fact": _str_param("Fact to remember")},
                "required": ["fact"],
                "additionalProperties": False,
            },
            risk="write",
            handler=_memory_remember,
            scope="memory:write",
            group=GROUP,
        )
    )


__all__ = ["GROUP", "register"]
