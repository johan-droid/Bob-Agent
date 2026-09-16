"""LLM task executor — wires the ReAct loop into the agent registry.

The RQ worker and the in-process orchestrator resolve agent handlers through
``agents/registry.py``. This module provides the ``llm`` handler: a real
ModelRouter invocation wrapped in ``services/agent_loop.run_tool_loop``, so
tasks execute with tools (shell, files, web, memory, MCP, OpenConnector)
instead of the deterministic echo builtin.

Honest fallback policy: with no real provider configured
(``default_provider`` is ``echo``/``none``/empty) unknown agent types keep
the deterministic builtin — tasks never pretend an LLM ran.
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Any

from agent_system.services.agent_loop import run_tool_loop
from agent_system.services.memory_hooks import recall_recent, remember_outcome
from agent_system.services.tools import ToolContext, build_registry

#: Providers that mean "no real model configured" — keep the echo builtin.
_ECHO_PROVIDERS = {"echo", "none", ""}

_GOAL_KEYS = ("goal", "prompt", "instruction", "description", "task", "title")

_SYSTEM_PROMPT = (
    "You are Bob, an autonomous agent. Complete the task below. "
    "Use tools when they help, reason step by step, and give a clear "
    "final answer when done."
)

_installed = False


def fallback_enabled(settings: Any) -> bool:
    """True when a real provider is configured (unknown types should use LLM)."""
    provider = str(getattr(settings, "default_provider", "echo") or "echo")
    return provider.strip().lower() not in _ECHO_PROVIDERS


def fallback_handler(task_input: dict[str, Any] | None = None) -> Any:
    """The LLM handler when a real provider is configured, else None.

    When ``task_input`` is given, the handler is only returned if the input
    actually carries a goal — tasks with nothing to reason about keep the
    deterministic builtin (or fail cleanly when nothing is registered).
    """
    from agent_system.config import get_settings

    if not fallback_enabled(get_settings()):
        return None
    if task_input is not None and _extract_goal(task_input) is None:
        return None
    return llm_react_handler


def dispatch_default(task_input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Goal-aware registry default: LLM loop when there is something to do.

    Registered via ``install()`` as the fallback for unregistered agent
    types when a real provider is configured; inputs without a goal keep
    the honest deterministic builtin.
    """
    from agent_system.agents import registry

    if _extract_goal(task_input) is not None:
        return llm_react_handler(task_input, context)
    return registry.builtin(task_input, context)


def install() -> None:
    """Register the ``llm`` handler (idempotent; safe to call per task).

    With a real provider configured, the LLM handler also becomes the
    registry's default so previously-unhandled agent types execute for real.
    """
    global _installed
    from agent_system.agents import registry
    from agent_system.config import get_settings

    registry.register("llm", llm_react_handler)
    if fallback_enabled(get_settings()):
        # Explicit, recorded fallback: routing an unsupported agent type to the
        # generic agent emits `agent.fallback_applied` instead of happening
        # silently (v3.1 §4).
        registry.set_fallback(dispatch_default, reason="unsupported_agent_type")
    else:
        registry.set_fallback(None)
    _installed = True


def _extract_goal(task_input: dict[str, Any]) -> str | None:
    """First non-empty goal-ish key, else the whole input as JSON, else None."""
    for key in _GOAL_KEYS:
        value = task_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if task_input:
        return _json.dumps(task_input, default=str)[:2000]
    return None


def _with_memory(settings: Any, goal: str, factory: Any = None) -> str:
    """Inject the top-k most relevant vault notes into the task text."""
    top_k = int(getattr(settings, "memory_recall_top_k", 0) or 0)
    if top_k <= 0:
        return goal
    try:
        notes = recall_recent(settings, goal, top_k, factory=factory)
    except Exception:
        return goal  # memory must never break execution
    if not notes:
        return goal
    rendered = "\n".join(f"- {n['title']}: {n['snippet'][:200]}" for n in notes)
    return f"{goal}\n\nRelevant memories:\n{rendered}"


def _build_router(settings: Any, bus: Any) -> Any:
    """ModelRouter with every configured provider adapter + skills + soul.

    Self-healing for offline mode: when the default model has no pricing
    entry or adapter (e.g. ``default_provider=echo`` with nothing else
    configured), a zero-cost echo entry + EchoProvider are registered so
    the loop is actually runnable instead of failing with 'no adapter'.
    """
    from agent_system.services.model_router import EchoProvider, ModelInfo
    from agent_system.services.providers import build_model_router
    from agent_system.services.skills import SkillManager
    from agent_system.services.soul import load_soul

    skill_manager: Any = None
    builtin_dir: Path | None = None
    if Path(str(getattr(settings, "skills_dir", "skills"))) == Path("skills"):
        import agent_system as _pkg

        builtin_dir = Path(_pkg.__file__).resolve().parents[2] / "skills"
    try:
        skill_manager = SkillManager(settings.skills_dir, builtin_dir=builtin_dir)
    except Exception:
        skill_manager = None  # skills are optional
    try:
        _, soul_text = load_soul(getattr(settings, "soul_path", "") or None)
    except Exception:
        soul_text = ""
    router = build_model_router(
        bus, settings, skill_manager=skill_manager, soul_text=soul_text or None
    )
    default_id = str(router.default_model)
    if router.pricing.get(default_id) is None:
        router.pricing.register(
            ModelInfo(
                model_id=default_id,
                provider="echo",
                input_cost_per_1m=0.0,
                output_cost_per_1m=0.0,
            )
        )
    if router._adapter_for(default_id) is None:  # noqa: SLF001 — same-package seam
        router.register_adapter("echo", EchoProvider())
    return router


def llm_react_handler(task_input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Execute one task goal: reason + act with the tool registry until done.

    Context must carry the session ``factory`` (model calls, tool events and
    approvals are recorded in SQLite) plus the usual session/task/run ids.
    """
    from agent_system.config import get_settings
    from agent_system.domain.events import Event
    from agent_system.infra.db import session_scope
    from agent_system.infra.event_bus import EventBus
    from agent_system.services.providers import default_model_id

    settings = context.get("settings") or get_settings()
    factory = context.get("factory")
    if factory is None:
        raise ValueError("llm agent requires a session factory in the execution context")
    session_id = context.get("session_id")
    task_id = context.get("task_id")
    run_id = context.get("agent_run_id")
    agent_type = context.get("agent_type") or "llm"
    goal = _extract_goal(task_input)
    if goal is None:
        raise ValueError(
            f"no task goal found in task input (looked for keys: {', '.join(_GOAL_KEYS)})"
        )
    # Reuse the caller's bus when provided — sequence counters are per
    # instance, so a second bus in the same process would collide with it.
    bus = context.get("bus") or EventBus()

    def emit(event_type: str, payload: dict[str, Any]) -> None:
        try:
            with session_scope(factory) as db:
                bus.emit(
                    Event(
                        type=event_type,
                        session_id=session_id,
                        task_id=task_id,
                        agent_run_id=run_id,
                        actor=agent_type,
                        payload=payload,
                    ),
                    db,
                )
        except Exception:
            pass  # event emission must never break execution

    router = _build_router(settings, bus)
    model_id = str(task_input.get("model") or default_model_id(settings))

    def invoke(prompt: str) -> dict[str, Any]:
        def _on_token(delta: str) -> None:
            # Incremental tokens ride the same emit channel as every other
            # event (visibility=user by default) — the WS/SSE fanout and the
            # chat REPL render them live; model.completed still closes the call.
            emit("model.token", {"model_id": model_id, "delta": delta})

        if hasattr(router, "invoke_streaming"):
            result = router.invoke_streaming(
                factory,
                model_id,
                prompt,
                session_id=session_id,
                task_id=task_id,
                agent_run_id=run_id,
                agent_type=agent_type,
                on_token=_on_token,
            )
        else:  # pragma: no cover — all shipped routers stream
            result = router.invoke(
                factory,
                model_id,
                prompt,
                session_id=session_id,
                task_id=task_id,
                agent_run_id=run_id,
                agent_type=agent_type,
            )
        if not result.ok:
            raise RuntimeError(result.error or "model invocation failed")
        usage = {
            "input_tokens": int(result.tokens_in or 0),
            "output_tokens": int(result.tokens_out or 0),
            "cached_tokens": int(result.tokens_cached or 0),
        }
        return {"output": result.output or "", "usage": usage}

    tool_registry = build_registry(settings)
    # Capabilities resolve permissions through the shared gate: same durable
    # store the API serves, so a user approval unblocks the waiting call.
    from agent_system.services.permissions import PermissionGate

    gate = context.get("gate") or PermissionGate(factory=factory)
    tool_ctx = ToolContext(
        settings=settings,
        factory=factory,
        session_id=session_id,
        task_id=task_id,
        agent_run_id=run_id,
        agent_type=agent_type,
        workspace_id=context.get("workspace_id"),
        gate=gate,
        emit=emit,
    )
    loop = run_tool_loop(
        invoke=invoke,
        system=_SYSTEM_PROMPT,
        task=_with_memory(settings, goal, factory=factory),
        registry=tool_registry,
        ctx=tool_ctx,
        emit=emit,
        max_iters=int(getattr(settings, "tools_max_iters", 8) or 8),
        max_context_tokens=int(getattr(settings, "max_context_tokens", 100_000) or 100_000),
        compaction_threshold_pct=float(
            getattr(settings, "context_compaction_threshold_pct", 75.0) or 75.0
        ),
    )

    note_id = None
    if session_id and task_id and loop.stopped == "done":
        note_id = remember_outcome(
            settings, session_id, task_id, title=goal[:80], output=loop.output, factory=factory
        )
    return {
        "agent": run_id or "unknown",
        "task": task_id,
        "model": model_id,
        "output": loop.output,
        "stopped": loop.stopped,
        "tool_calls": loop.tool_calls,
        "iterations": loop.iterations,
        "usage": loop.usage,
        "memory_note_id": note_id,
    }


__all__ = [
    "dispatch_default",
    "fallback_enabled",
    "fallback_handler",
    "install",
    "llm_react_handler",
]
