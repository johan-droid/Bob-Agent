"""Recipe library (v3.1 Phase 16, §13 feature note).

A recipe is a versioned task DAG (JSON) + parameters. Execution rides the
canonical task pipeline — recipes create real Task rows through the
Supervisor and never bypass permissions or the event bus. One-click UX from
v3.0, v3.1 safety: DAG validation, parameter substitution, history,
cancellation.
"""

from __future__ import annotations

import re
from typing import Any

from agent_system.domain import ids
from agent_system.domain.events import Event
from agent_system.infra.db import session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Recipe, Task
from agent_system.services.orchestrator import Supervisor

MAX_STEPS_PER_RECIPE = 50
_PARAM_RE = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}")


class RecipeError(ValueError):
    pass


def _validate_dag(dag: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate shape + acyclicity; returns the task step list."""
    steps = dag.get("tasks")
    if not isinstance(steps, list) or not steps:
        raise RecipeError("recipe DAG must contain a non-empty 'tasks' list")
    if len(steps) > MAX_STEPS_PER_RECIPE:
        raise RecipeError(f"recipe exceeds {MAX_STEPS_PER_RECIPE} steps")
    keys: set[str] = set()
    for step in steps:
        if not isinstance(step, dict):
            raise RecipeError("each step must be an object")
        key = step.get("key")
        if not key or not isinstance(key, str):
            raise RecipeError("each step needs a 'key'")
        if key in keys:
            raise RecipeError(f"duplicate step key: {key}")
        if not step.get("task_type") or not step.get("title"):
            raise RecipeError(f"step '{key}' needs task_type and title")
        keys.add(key)
    known = keys
    for step in steps:
        for dep in step.get("depends_on", []) or []:
            if dep not in known:
                raise RecipeError(f"step '{step['key']}' depends on unknown '{dep}'")
    # Cycle detection (DFS).
    graph = {s["key"]: list(s.get("depends_on") or []) for s in steps}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise RecipeError(f"dependency cycle at '{node}'")
        if node in visited:
            return
        visiting.add(node)
        for dep in graph.get(node, []):
            visit(dep)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)
    return steps


class RecipeEngine:
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._supervisor = Supervisor(bus)

    def create_recipe(
        self,
        factory: Any,
        name: str,
        dag: dict[str, Any],
        parameters: dict[str, Any] | None = None,
        description: str = "",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        steps = _validate_dag(dag)
        recipe_id = ids.new_recipe_id()
        with session_scope(factory) as db:
            db.add(
                Recipe(
                    id=recipe_id,
                    name=name,
                    description=description,
                    task_dag_json={"tasks": steps},
                    parameters_json=parameters or {},
                    tags_json=tags or [],
                    version=1,
                )
            )
        return {
            "recipe_id": recipe_id,
            "name": name,
            "version": 1,
            "steps": len(steps),
            "parameters": parameters or {},
        }

    def get_recipe(self, factory: Any, recipe_id: str) -> dict[str, Any] | None:
        with session_scope(factory) as db:
            r = db.get(Recipe, recipe_id)
            if r is None:
                return None
            return self._recipe_out(r)

    def list_recipes(self, factory: Any) -> list[dict[str, Any]]:
        with session_scope(factory) as db:
            rows = db.query(Recipe).order_by(Recipe.created_at.desc()).all()
            return [self._recipe_out(r) for r in rows]

    def execute(
        self, factory: Any, recipe_id: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Instantiate the DAG as real tasks in a new session and run it
        through the canonical Supervisor pipeline."""
        with session_scope(factory) as db:
            recipe = db.get(Recipe, recipe_id)
            if recipe is None:
                raise RecipeError(f"recipe {recipe_id} not found")
            dag = recipe.task_dag_json
            merged = {**recipe.parameters_json, **(params or {})}
            name = recipe.name
        steps = dag.get("tasks", [])
        unresolved = _missing_params(steps, merged)
        if unresolved:
            raise RecipeError(f"missing recipe parameters: {sorted(unresolved)}")

        session_id = self._supervisor.create_session(factory, f"recipe:{name}")
        key_to_task: dict[str, str] = {}
        # Create tasks in dependency order (topological via repeated passes).
        pending = list(steps)
        while pending:
            progressed = False
            for step in list(pending):
                deps = [key_to_task[d] for d in (step.get("depends_on") or []) if d in key_to_task]
                if len(deps) != len(set(step.get("depends_on") or [])):
                    continue
                title = _substitute(str(step["title"]), merged)
                task_input = _substitute_deep(step.get("input") or {}, merged)
                key_to_task[step["key"]] = self._supervisor.add_task(
                    factory,
                    session_id,
                    task_type=str(step["task_type"]),
                    title=title,
                    input_json=task_input,
                    depends_on=deps,
                    agent_type=step.get("agent_type"),
                )
                pending.remove(step)
                progressed = True
            if not progressed:
                raise RecipeError("recipe DAG could not be ordered (cycle?)")

        with session_scope(factory) as db:
            recipe_row = db.get(Recipe, recipe_id)
            if recipe_row is not None:
                recipe_row.executions_count += 1
        return {
            "recipe_id": recipe_id,
            "session_id": session_id,
            "task_ids": [key_to_task[s["key"]] for s in steps],
            "version": dag.get("version", 1),
        }

    def cancel_run(self, factory: Any, recipe_id: str) -> dict[str, Any]:
        """Cancel the most recent run's still-cancellable tasks."""
        with session_scope(factory) as db:
            recipe = db.get(Recipe, recipe_id)
            if recipe is None:
                raise RecipeError(f"recipe {recipe_id} not found")
        # Find tasks belonging to the latest recipe session (marked in title prefix).
        cancelled: list[str] = []
        with session_scope(factory) as db:
            sessions = (
                db.query(Task.session_id)
                .filter(Task.title.like("recipe:%"))
                .order_by(Task.created_at.desc())
                .first()
            )
        if sessions is None:
            return {"recipe_id": recipe_id, "cancelled": []}
        session_id = sessions[0]
        from agent_system.domain.tasks import TaskState, validate_transition

        with session_scope(factory) as db:
            for task in db.query(Task).filter_by(session_id=session_id).all():
                if task.state in (TaskState.PENDING.value, TaskState.QUEUED.value):
                    validate_transition(TaskState(task.state), TaskState.CANCELLED)
                    task.state = TaskState.CANCELLED.value
                    cancelled.append(task.id)
                    self._bus.emit(
                        Event(type="task.cancelled", task_id=task.id, actor="recipe"), db
                    )
        return {"recipe_id": recipe_id, "session_id": session_id, "cancelled": cancelled}

    @staticmethod
    def _recipe_out(r: Recipe) -> dict[str, Any]:
        return {
            "recipe_id": r.id,
            "name": r.name,
            "description": r.description,
            "version": r.version,
            "steps": len(r.task_dag_json.get("tasks", [])),
            "parameters": r.parameters_json,
            "tags": r.tags_json,
            "executions": r.executions_count,
        }


def _missing_params(steps: list[dict[str, Any]], params: dict[str, Any]) -> set[str]:
    missing: set[str] = set()
    for step in steps:
        for text in (str(step.get("title", "")), _dumps(step.get("input") or {})):
            for name in _PARAM_RE.findall(text):
                if name not in params:
                    missing.add(name)
    return missing


def _dumps(obj: Any) -> str:
    import json

    return json.dumps(obj) if obj else ""


def _substitute(text: str, params: dict[str, Any]) -> str:
    return _PARAM_RE.sub(lambda m: str(params.get(m.group(1), m.group(0))), text)


def _substitute_deep(obj: Any, params: dict[str, Any]) -> Any:
    if isinstance(obj, str):
        return _substitute(obj, params)
    if isinstance(obj, dict):
        return {k: _substitute_deep(v, params) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute_deep(v, params) for v in obj]
    return obj
