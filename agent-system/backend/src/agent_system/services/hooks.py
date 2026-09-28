"""General-purpose hooks system — deterministic lifecycle callbacks.

Hooks provide deterministic behavior around the non-deterministic agent loop.
They fire at well-defined points:

    SessionStart, UserPrompt, BeforeTool, AfterTool,
    BeforeEdit, AfterEdit, BeforeCommit, AfterTest,
    TaskComplete, TaskFailed

Each hook is a sync or async callable that receives a context dict and can
return a modified context or a signal to abort. Hooks are registered by name
and event type, and fire in registration order.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class HookEvent(StrEnum):
    """Well-defined hook points in the agent lifecycle."""

    SESSION_START = "session_start"
    USER_PROMPT = "user_prompt"
    BEFORE_TOOL = "before_tool"
    AFTER_TOOL = "after_tool"
    BEFORE_EDIT = "before_edit"
    AFTER_EDIT = "after_edit"
    BEFORE_COMMIT = "before_commit"
    AFTER_TEST = "after_test"
    TASK_COMPLETE = "task_complete"
    TASK_FAILED = "task_failed"


class HookSignal(StrEnum):
    """Signals a hook can return to control flow."""

    CONTINUE = "continue"
    ABORT = "abort"
    RETRY = "retry"
    SKIP = "skip"


@dataclass
class HookContext:
    """Context passed to every hook."""

    event: HookEvent
    data: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    signal: HookSignal = HookSignal.CONTINUE
    abort_reason: str = ""


#: A hook is any callable that takes a HookContext and returns a HookContext.
HookFn = Callable[[HookContext], HookContext]


@dataclass
class HookRegistration:
    """One registered hook."""

    name: str
    event: HookEvent
    fn: HookFn
    priority: int = 100
    enabled: bool = True


class HookManager:
    """Manages hook registration and firing."""

    def __init__(self) -> None:
        self._hooks: dict[HookEvent, list[HookRegistration]] = {e: [] for e in HookEvent}

    def register(
        self,
        event: HookEvent,
        fn: HookFn,
        *,
        name: str = "",
        priority: int = 100,
    ) -> str:
        """Register a hook for an event. Lower priority fires first."""
        hook_name = name or f"hook_{len(self._hooks[event])}"
        reg = HookRegistration(
            name=hook_name,
            event=event,
            fn=fn,
            priority=priority,
        )
        self._hooks[event].append(reg)
        self._hooks[event].sort(key=lambda r: r.priority)
        return hook_name

    def unregister(self, event: HookEvent, name: str) -> bool:
        """Remove a hook by name. Returns True if found and removed."""
        hooks = self._hooks[event]
        for i, reg in enumerate(hooks):
            if reg.name == name:
                hooks.pop(i)
                return True
        return False

    def disable(self, event: HookEvent, name: str) -> bool:
        """Disable a hook without removing it."""
        for reg in self._hooks[event]:
            if reg.name == name:
                reg.enabled = False
                return True
        return False

    def enable(self, event: HookEvent, name: str) -> bool:
        """Re-enable a disabled hook."""
        for reg in self._hooks[event]:
            if reg.name == name:
                reg.enabled = True
                return True
        return False

    async def fire(self, event: HookEvent, data: dict[str, Any] | None = None) -> HookContext:
        """Fire all hooks for an event. Returns the final context.

        If any hook returns ABORT, subsequent hooks are skipped and the
        abort signal propagates.
        """
        ctx = HookContext(event=event, data=data or {})
        for reg in self._hooks[event]:
            if not reg.enabled:
                continue
            try:
                result = reg.fn(ctx)
                if asyncio.iscoroutine(result):
                    result = await result
                if result is not None:
                    ctx = result
                if ctx.signal == HookSignal.ABORT:
                    logger.info(
                        "hook.abort event=%s hook=%s reason=%s", event, reg.name, ctx.abort_reason
                    )
                    break
            except Exception as exc:
                logger.warning("hook.error event=%s hook=%s error=%s", event, reg.name, exc)
        return ctx

    def fire_sync(self, event: HookEvent, data: dict[str, Any] | None = None) -> HookContext:
        """Fire hooks synchronously (raises if a hook is async)."""
        ctx = HookContext(event=event, data=data or {})
        for reg in self._hooks[event]:
            if not reg.enabled:
                continue
            try:
                result = reg.fn(ctx)
                if asyncio.iscoroutine(result):
                    raise RuntimeError(
                        f"Hook '{reg.name}' is async but fire_sync was called. Use fire() instead."
                    )
                if result is not None:
                    ctx = result
                if ctx.signal == HookSignal.ABORT:
                    break
            except Exception as exc:
                logger.warning("hook.error event=%s hook=%s error=%s", event, reg.name, exc)
        return ctx

    def list_hooks(self, event: HookEvent | None = None) -> list[HookRegistration]:
        """List registered hooks, optionally filtered by event."""
        if event is not None:
            return list(self._hooks[event])
        return [reg for hooks in self._hooks.values() for reg in hooks]

    def clear(self, event: HookEvent | None = None) -> None:
        """Clear all hooks, or just for one event."""
        if event is not None:
            self._hooks[event] = []
        else:
            for e in self._hooks:
                self._hooks[e] = []


#: Module-level singleton.
_default_manager: HookManager | None = None


def get_hook_manager() -> HookManager:
    """Get or create the default hook manager."""
    global _default_manager
    if _default_manager is None:
        _default_manager = HookManager()
    return _default_manager


def reset_hook_manager() -> None:
    """Reset the default hook manager (for tests)."""
    global _default_manager
    _default_manager = None


__all__ = [
    "HookContext",
    "HookEvent",
    "HookFn",
    "HookManager",
    "HookRegistration",
    "HookSignal",
    "get_hook_manager",
    "reset_hook_manager",
]
