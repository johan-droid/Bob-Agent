"""Agent handler registry (Phase 4+): agent_type -> execution function.

Built-in handlers cover the deterministic agent types the system can execute
without external services. External integrations (browser via Playwright,
documents in sandbox, autopilot) register richer handlers at composition
time; this module is the fallback resolution point for the RQ worker.
"""

from __future__ import annotations

import time
from typing import Any

_REGISTRY: dict[str, Any] = {}


def register(agent_type: str, handler: Any) -> None:
    """Register a handler for an agent type (composition-time extension)."""
    _REGISTRY[agent_type] = handler


def register_default(handler: Any) -> None:
    """Replace the fallback handler used for unregistered agent types."""
    global _DEFAULT_HANDLER
    _DEFAULT_HANDLER = handler


def handler_for(agent_type: str) -> Any:
    return _REGISTRY.get(agent_type, _DEFAULT_HANDLER)


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
    }


_DEFAULT_HANDLER: Any = _builtin

#: Public alias — composition modules (e.g. react_agent.dispatch_default)
#: route goal-less tasks back to the deterministic builtin.
builtin = _builtin


def run_agent(
    agent_type: str, task_input: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """Resolve and invoke the handler for an agent type."""
    handler = handler_for(agent_type)
    result: dict[str, Any] = handler(task_input, context)
    return result
