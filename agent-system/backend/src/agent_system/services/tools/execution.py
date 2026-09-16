"""The single capability execution path (v3.1 §17).

    model tool call
      -> parse                    (services/tools/protocol.py)
      -> tool exists?             (registry lookup)
      -> arguments schema         (schemas.validate_arguments)
      -> permission evaluation    (permissions.require_capability)
      -> execution                (tool.handler)

Every capability invocation in the system goes through :func:`execute_tool`.
Individual capabilities do not evaluate permissions: they declare a risk tier
and a scope, and this module resolves both. A handler is never entered with
arguments that failed validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_system.services.permissions import (
    ApprovalRecord,
    CapabilityRisk,
    require_capability,
)
from agent_system.services.tool_errors import ToolError, ToolValidationError
from agent_system.services.tools.registry import Tool, ToolContext
from agent_system.services.tools.schemas import validate_arguments


@dataclass(frozen=True)
class PermissionPlan:
    """The documented permission decision for one capability call."""

    scope: str
    tier: CapabilityRisk
    requires_approval: bool
    default_deny: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "capability_risk": self.tier.value,
            "requires_approval": self.requires_approval,
            "default_deny": self.default_deny,
            "reason": self.reason,
        }


def plan_permission(tool: Tool, args: dict[str, Any], settings: Any = None) -> PermissionPlan:
    """Resolve the permission decision for a capability before it executes."""
    from agent_system.services.permissions import is_dangerous_scope

    scope = tool.scope_for(args)
    tier = tool.tier
    if tier is CapabilityRisk.DESTRUCTIVE:
        return PermissionPlan(
            scope=scope,
            tier=tier,
            requires_approval=True,
            default_deny=True,
            reason=tool.destructive_reason or "destructive capabilities are default-deny",
        )
    if is_dangerous_scope(scope):
        return PermissionPlan(
            scope=scope,
            tier=tier,
            requires_approval=True,
            default_deny=True,
            reason="scope is in DANGEROUS_SCOPES (default-deny)",
        )
    required = tool.permission_required(settings)
    return PermissionPlan(
        scope=scope,
        tier=tier,
        requires_approval=required,
        default_deny=False,
        reason=(
            "read-only capability"
            if not required
            else "write/execute capability requires a live approval"
        ),
    )


def authorize_tool(tool: Tool, args: dict[str, Any], ctx: ToolContext) -> ApprovalRecord | None:
    """Ask the permission gate. Returns the grant, or raises."""
    plan = plan_permission(tool, args, getattr(ctx, "settings", None))
    if plan.default_deny and plan.tier is CapabilityRisk.DESTRUCTIVE:
        # Destructive capabilities are refused regardless of the settings
        # toggle: there is no configuration in which they simply run.
        return require_capability(
            ctx,
            scope=plan.scope,
            action=f"{tool.name} {plan.scope}",
            capability_risk=CapabilityRisk.DESTRUCTIVE,
        )
    if not plan.requires_approval:
        return None
    return require_capability(
        ctx,
        scope=plan.scope,
        action=f"{tool.name} {plan.scope}",
        capability_risk=plan.tier,
    )


def execute_tool(tool: Tool, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Validate, authorize, then run one capability call.

    Raises :class:`ToolValidationError` before the handler is entered, and
    propagates :class:`NeedsApprovalError` when a live approval is required.
    """
    errors = validate_arguments(tool.name, tool.parameters, args)
    if errors:
        raise ToolValidationError(tool.name, errors)
    authorize_tool(tool, args, ctx)
    result = tool.handler(args, ctx)
    if result is None:
        return {}
    if not isinstance(result, dict):
        raise ToolError(f"capability '{tool.name}' returned {type(result).__name__}, expected dict")
    return result


__all__ = ["PermissionPlan", "authorize_tool", "execute_tool", "plan_permission"]
