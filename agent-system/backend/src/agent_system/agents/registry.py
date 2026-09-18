"""Agent registry — explicit definitions, explicit fallback (v3.1 §3, §4).

Resolution rules (see ``agents/definitions.py`` for the declarations):

1. A registered agent type runs its own handler under its own definition.
2. An unregistered type runs the **generic** agent *only* when a fallback
   handler has been installed explicitly, and the resolution records
   ``fallback_reason="unsupported_agent_type"``.
3. Without an installed fallback, an unregistered type raises
   :class:`UnknownAgentTypeError` — it never silently becomes LLM execution.
4. Every fallback that actually runs emits ``agent.fallback_applied`` so the
   routing decision is visible in the canonical event stream.

The honest deterministic builtin remains the fallback for tasks with nothing to
reason about (no goal in the input); it fabricates no work and calls no model.
"""

from __future__ import annotations

import time
from typing import Any

from agent_system.agents.definitions import (
    DETERMINISTIC,
    AgentDefinition,
    AgentResolution,
    ad_hoc_definition,
    definition_for,
)

_HANDLERS: dict[str, Any] = {}
_DEFINITIONS: dict[str, AgentDefinition] = {}
_FALLBACK: Any = None
_FALLBACK_REASON: str | None = None


class UnknownAgentTypeError(LookupError):
    """Raised when an agent type has no handler and no explicit fallback applies."""

    def __init__(self, agent_type: str, known: list[str]) -> None:
        self.agent_type = agent_type
        super().__init__(
            f"no handler registered for agent type '{agent_type}' "
            f"(known: {', '.join(sorted(known)) or 'none'}; "
            "install a fallback handler to route unsupported types to generic)"
        )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(agent_type: str, handler: Any, definition: AgentDefinition | None = None) -> None:
    """Register a handler for an agent type (composition-time extension).

    The definition defaults to the declared catalog entry for that name, or an
    ad-hoc definition describing the dynamic registration — never to a silent
    placeholder with no policy.
    """
    _HANDLERS[agent_type] = handler
    _DEFINITIONS[agent_type] = (
        definition or definition_for(agent_type) or ad_hoc_definition(agent_type)
    )


def register_default(handler: Any, reason: str = "unsupported_agent_type") -> None:
    """Install the explicit fallback handler for unsupported agent types.

    Naming is preserved for compatibility; the semantics are now *explicit
    opt-in with a recorded reason*, not a hidden default.
    """
    set_fallback(handler, reason=reason)


def set_fallback(handler: Any, reason: str = "unsupported_agent_type") -> None:
    """Install (or clear, with ``handler=None``) the generic fallback handler."""
    global _FALLBACK, _FALLBACK_REASON
    _FALLBACK = handler
    _FALLBACK_REASON = reason if handler is not None else None


def fallback_installed() -> bool:
    return _FALLBACK is not None


def handler_for(agent_type: str) -> Any:
    """The registered handler for a type, else the deterministic builtin.

    Reading the *handler* for an unknown type must stay side-effect free (the
    tests probe this), so the routing decision — including whether a fallback
    is acceptable — lives in :func:`resolve`, which is what actually dispatches.
    """
    return _HANDLERS.get(agent_type, _builtin)


def registered_types() -> list[str]:
    return sorted(_HANDLERS)


def snapshot() -> dict[str, Any]:
    """Copy the registry state (public seam for tests / composition resets)."""
    return {
        "handlers": dict(_HANDLERS),
        "definitions": dict(_DEFINITIONS),
        "fallback": _FALLBACK,
        "fallback_reason": _FALLBACK_REASON,
    }


def restore(state: dict[str, Any]) -> None:
    """Restore a state captured by :func:`snapshot`."""
    global _FALLBACK, _FALLBACK_REASON
    _HANDLERS.clear()
    _HANDLERS.update(state.get("handlers") or {})
    _DEFINITIONS.clear()
    _DEFINITIONS.update(state.get("definitions") or {})
    _FALLBACK = state.get("fallback")
    _FALLBACK_REASON = state.get("fallback_reason")


def definitions() -> dict[str, AgentDefinition]:
    """Declared definitions for every registered agent (inventory surface)."""
    merged = {agent_type: definition for agent_type, definition in _DEFINITIONS.items()}
    return merged


def inventory() -> list[dict[str, Any]]:
    """Definitions plus their registration state, for docs and ``/agents``."""
    return [
        {**definition.to_json(), "registered": True}
        for _, definition in sorted(_DEFINITIONS.items())
    ]


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve(agent_type: str, task_input: dict[str, Any] | None = None) -> AgentResolution:
    """Decide which definition/handler runs this task, and why.

    Raises :class:`UnknownAgentTypeError` when the type is unregistered and no
    explicit fallback is installed.
    """
    requested = (agent_type or "").strip() or "generic"
    if requested in _HANDLERS:
        return AgentResolution(
            requested_type=agent_type,
            definition=_DEFINITIONS.get(requested)
            or definition_for(requested)
            or ad_hoc_definition(requested),
            handler=_HANDLERS[requested],
        )
    if _FALLBACK is None:
        raise UnknownAgentTypeError(agent_type, known=registered_types())
    return AgentResolution(
        requested_type=agent_type,
        definition=definition_for("generic") or ad_hoc_definition("generic"),
        handler=_FALLBACK,
        fallback_reason=_FALLBACK_REASON or "unsupported_agent_type",
    )


def _emit_fallback(context: dict[str, Any], resolution: AgentResolution) -> None:
    """Record a fallback routing decision in the canonical event stream."""
    bus = context.get("bus")
    factory = context.get("factory")
    if bus is None or factory is None:
        return
    try:
        from agent_system.domain.events import Event
        from agent_system.infra.db import session_scope

        with session_scope(factory) as db:
            bus.emit(
                Event(
                    type="agent.fallback_applied",
                    session_id=context.get("session_id"),
                    task_id=context.get("task_id"),
                    agent_run_id=context.get("agent_run_id"),
                    actor=context.get("agent_type") or "router",
                    payload={
                        "requested_type": resolution.requested_type,
                        "resolved_agent": resolution.definition.name,
                        "fallback_reason": resolution.fallback_reason,
                    },
                ),
                db,
            )
    except Exception:
        pass  # routing must not fail because the audit write did


def run_agent(
    agent_type: str,
    task_input: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Resolve and invoke the handler for an agent type.

    The result carries a ``_routing`` block describing how the call was routed,
    so a caller can always tell whether it ran the requested agent or a fallback.
    """
    resolution = resolve(agent_type, task_input)
    if resolution.is_fallback:
        _emit_fallback(context, resolution)
    result: dict[str, Any] = resolution.handler(task_input, context)
    if isinstance(result, dict) and "_routing" not in result:
        result["_routing"] = resolution.to_json()
    return result


def _builtin(task_input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Deterministic built-in execution: sleeps per spec, echoes context.

    Used when no richer handler is registered. Honest about what it did —
    never fabricates work.
    """
    duration = float(task_input.get("simulate_seconds", 0))
    if duration > 0:
        time.sleep(min(duration, 30.0))
    return {
        "agent": context.get("agent_run_id", "unknown"),
        "task": context.get("task_id"),
        "simulated_seconds": duration,
        "input_keys": sorted(task_input.keys()),
        "execution_mode": DETERMINISTIC.execution_mode,
    }


#: Public alias — composition modules (e.g. react_agent.dispatch_default)
#: route goal-less tasks back to the deterministic builtin.
builtin = _builtin

# Register demo agent type for test execution compatibility
register("demo", _builtin)


__all__ = [
    "UnknownAgentTypeError",
    "builtin",
    "definitions",
    "fallback_installed",
    "handler_for",
    "inventory",
    "register",
    "register_default",
    "registered_types",
    "resolve",
    "restore",
    "run_agent",
    "set_fallback",
    "snapshot",
]
