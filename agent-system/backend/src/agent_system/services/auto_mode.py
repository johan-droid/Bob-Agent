"""Auto Mode — risk-gated autonomous operation.

Auto Mode lets Bob operate autonomously within risk boundaries:

    LOW RISK    -> AUTO (read, search, grep, run tests)
    MEDIUM RISK -> AUTO + guardrail (large refactor, delete generated files)
    HIGH RISK   -> ASK USER (production deploy, credentials, force push)

The mode is enforced by Jev (deterministic) and the permission gate.
This module provides the high-level AutoMode controller that orchestrates
the autonomous loop: plan -> execute -> verify -> report.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from agent_system.services.hooks import HookEvent, HookSignal, get_hook_manager
from agent_system.services.jev import Environment, Jev, JevDecision, ToolCategory
from agent_system.services.permissions import AutonomyMode, Risk

logger = logging.getLogger(__name__)


class AutoModeState(StrEnum):
    """States of the auto mode loop."""

    IDLE = "idle"
    PLANNING = "planning"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class StepResult:
    """Result of one auto mode step."""

    step: str
    success: bool
    output: str = ""
    error: str = ""
    tool_calls: int = 0
    duration_ms: float = 0.0


@dataclass
class AutoModeResult:
    """Result of an auto mode run."""

    goal: str
    state: AutoModeState
    steps: list[StepResult] = field(default_factory=list)
    total_tool_calls: int = 0
    total_duration_ms: float = 0.0
    approved_actions: int = 0
    denied_actions: int = 0
    asked_actions: int = 0
    final_output: str = ""
    error: str = ""


class AutoMode:
    """Risk-gated autonomous operation controller.

    Usage:
        auto = AutoMode(jev=get_jev(), environment=Environment.DEVELOPMENT)
        result = await auto.run("Add input validation to auth.py")
    """

    def __init__(
        self,
        *,
        jev: Jev | None = None,
        environment: Environment = Environment.DEVELOPMENT,
        autonomy_mode: AutonomyMode = AutonomyMode.AUTO,
        max_steps: int = 50,
        max_approvals: int = 10,
    ) -> None:
        self.jev = jev or Jev(environment=environment, autonomy_mode=autonomy_mode)
        self.environment = environment
        self.autonomy_mode = autonomy_mode
        self.max_steps = max_steps
        self.max_approvals = max_approvals
        self._state = AutoModeState.IDLE
        self._hooks = get_hook_manager()

    @property
    def state(self) -> AutoModeState:
        return self._state

    async def run(self, goal: str, context: dict[str, Any] | None = None) -> AutoModeResult:
        """Execute an autonomous run toward a goal.

        This is the high-level entry point. The actual step execution is
        delegated to the agent loop; this controller manages the risk gating,
        approval flow, and verification.
        """
        result = AutoModeResult(goal=goal, state=AutoModeState.PLANNING)

        # Fire session start hook
        hook_ctx = await self._hooks.fire(
            HookEvent.SESSION_START,
            {"goal": goal, "environment": self.environment.value},
        )
        if hook_ctx.signal == HookSignal.ABORT:
            result.state = AutoModeState.CANCELLED
            result.error = hook_ctx.abort_reason
            return result

        self._state = AutoModeState.EXECUTING

        # The actual execution is done by the agent loop + tool system.
        # AutoMode provides the risk-gated wrapper around each step.
        # For now, we return the planning result; the orchestrator
        # calls back into AutoMode for each step's risk check.

        result.state = AutoModeState.COMPLETED
        result.final_output = f"Auto mode completed for: {goal}"
        return result

    def check_action(
        self,
        *,
        tool_name: str,
        tool_category: ToolCategory,
        arguments: dict[str, Any] | None = None,
        scope: str = "",
        target: str = "",
        is_destructive: bool = False,
        is_remote: bool = False,
        affects_shared_state: bool = False,
    ) -> JevDecision:
        """Check if an action is allowed under current auto mode settings.

        Returns the Jev decision. The caller is responsible for acting on
        ASK (prompting the user) and DENY (refusing the action).
        """
        verdict = self.jev.evaluate_tool_call(
            tool_name=tool_name,
            tool_category=tool_category,
            arguments=arguments,
            scope=scope,
            target=target,
            is_destructive=is_destructive,
            is_remote=is_remote,
            affects_shared_state=affects_shared_state,
        )
        return verdict.decision

    def should_verify(self, risk: Risk) -> bool:
        """Determine if a step with given risk should be verified."""
        if self.environment == Environment.PRODUCTION:
            return True
        if risk in {Risk.HIGH, Risk.CRITICAL}:
            return True
        return False

    def reset(self) -> None:
        """Reset state for a new run."""
        self._state = AutoModeState.IDLE


#: Module-level singleton.
_default_auto: AutoMode | None = None


def get_auto_mode() -> AutoMode:
    """Get or create the default AutoMode instance."""
    global _default_auto
    if _default_auto is None:
        _default_auto = AutoMode()
    return _default_auto


def reset_auto_mode() -> None:
    """Reset the default AutoMode (for tests)."""
    global _default_auto
    _default_auto = None


__all__ = [
    "AutoMode",
    "AutoModeResult",
    "AutoModeState",
    "StepResult",
    "get_auto_mode",
    "reset_auto_mode",
]
