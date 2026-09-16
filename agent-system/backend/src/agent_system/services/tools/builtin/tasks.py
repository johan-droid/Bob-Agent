"""Task capabilities — read-only inspection of the canonical task store.

Deliberately read-only: an agent may inspect the task graph it is part of, but
it may not create, mutate or cancel tasks directly. Task lifecycle changes go
through the Supervisor/Orchestrator, which own the transition table and the
event emission — an agent quietly writing a task row would bypass both.
"""

from __future__ import annotations

from typing import Any

from agent_system.services.tool_errors import ToolError
from agent_system.services.tools.registry import Tool, ToolContext, ToolRegistry, _str_param

GROUP = "tasks"


def _tasks_inspect(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if ctx.factory is None:
        raise ToolError("task inspection needs a database session factory")
    from agent_system.infra.db import session_scope
    from agent_system.infra.models import Task as TaskRow

    session_id = str(args.get("session_id") or ctx.session_id or "")
    with session_scope(ctx.factory) as db:
        query = db.query(TaskRow)
        if session_id:
            query = query.filter_by(session_id=session_id)
        rows = query.order_by(TaskRow.created_at.desc()).limit(20).all()
        return {
            "session_id": session_id or None,
            "count": len(rows),
            "tasks": [
                {
                    "id": row.id,
                    "title": row.title,
                    "state": row.state,
                    "agent_type": row.agent_type,
                    "attempt": row.attempt,
                    "depends_on": row.depends_on_json or [],
                }
                for row in rows
            ],
        }


def _task_status(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        raise ToolError("task_status: 'task_id' is required")
    if ctx.factory is None:
        raise ToolError("task_status needs a database session factory")
    from agent_system.infra.db import session_scope
    from agent_system.infra.models import AgentRun
    from agent_system.infra.models import Task as TaskRow

    with session_scope(ctx.factory) as db:
        task = db.get(TaskRow, task_id)
        if task is None:
            raise ToolError(f"no task with id {task_id}")
        runs = db.query(AgentRun).filter_by(task_id=task_id).all()
        return {
            "task_id": task.id,
            "state": task.state,
            "title": task.title,
            "agent_type": task.agent_type,
            "attempt": task.attempt,
            "last_error": task.last_error,
            "depends_on": task.depends_on_json or [],
            "result_keys": sorted((task.result_json or {}).keys()),
            "runs": [
                {"agent_run_id": r.id, "state": r.state, "agent_type": r.agent_type} for r in runs
            ],
        }


def register(registry: ToolRegistry, settings: Any = None) -> None:  # noqa: ARG001
    registry.register(
        Tool(
            name="tasks_inspect",
            description="List recent tasks (optionally for one session).",
            parameters={
                "type": "object",
                "properties": {"session_id": _str_param("Session id (default: current session)")},
                "required": [],
                "additionalProperties": False,
            },
            risk="read",
            handler=_tasks_inspect,
            group=GROUP,
        )
    )
    registry.register(
        Tool(
            name="task_status",
            description="Show one task's state, attempts and agent runs.",
            parameters={
                "type": "object",
                "properties": {"task_id": _str_param("Task id")},
                "required": ["task_id"],
                "additionalProperties": False,
            },
            risk="read",
            handler=_task_status,
            group=GROUP,
        )
    )


__all__ = ["GROUP", "register"]
