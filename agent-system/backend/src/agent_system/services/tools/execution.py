"""The single capability execution path (v3.1 §17).

    model tool call
      -> parse                    (services/tools/protocol.py)
      -> tool exists?             (registry lookup)
      -> arguments schema         (schemas.validate_arguments)
      -> policy evaluation        (policy.PolicyEngine)
      -> execution                (tool.handler)

Every capability invocation in the system goes through :func:`execute_tool`.
Individual capabilities do not evaluate permissions: they declare a risk tier
and a scope, and this module resolves both. A handler is never entered with
arguments that failed validation.

The physical invocation of ``tool.handler`` lives in exactly one place —
:func:`_invoke_handler`. Every entry point (:func:`execute_tool`,
:func:`execute_request`, :func:`execute_with_policy`,
:func:`execute_request_with_policy`, :func:`run_tool_call`,
:func:`run_request`) funnels the run step through it; no caller enters a
handler directly. Policy-aware paths authorize once through the
:class:`PolicyEngine` and then reuse the same run seam — they never inline
execution, so the authorization and the handler call cannot drift.

Two typed entry points sit on top of that same path (Phase 1 tool contract):

- :func:`decide` — the *pure* policy verdict (:class:`ExecutionDecision`);
  it never touches the gate, so callers can inspect a call without side effects.
- :func:`execute_request` — runs a request and returns an :class:`ExecutionResult`
  value instead of raising, reporting the *effective* decision after the live
  gate has run. It delegates to :func:`execute_tool`; it is not a second path.

Phase 3: Policy Engine integration. The :class:`PolicyEngine` centralizes
risk, scope, approval, sandbox, identity, resource, and timeout evaluation.
Use :func:`execute_with_policy` for the new unified path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from agent_system.services.permissions import (
    ApprovalRecord,
    CapabilityRisk,
    classify_risk,
    require_capability,
)
from agent_system.services.policy import (
    PolicyContext,
    PolicyDecision,
    PolicyEngine,
    PolicyVerdict,
    get_policy_engine,
)
from agent_system.services.tool_errors import (
    NeedsApprovalError,
    ToolError,
    ToolValidationError,
)
from agent_system.services.tools.contract import (
    CapabilityMetadata,
    ExecutionDecision,
    ExecutionOutcome,
    ExecutionRequest,
    ExecutionResult,
)
from agent_system.services.tools.protocol import ToolCall
from agent_system.services.tools.registry import Tool, ToolContext, ToolKind, ToolRegistry
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


def _invoke_handler(tool: Tool, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """The single physical handler invocation point (private).

    No caller ever enters ``tool.handler`` directly — every entry point in
    this module funnels the run step through here. Validation and
    authorization are performed upstream; this helper only runs the
    capability and normalizes the result.
    """
    result = tool.handler(args, ctx)
    if result is None:
        return {}
    if not isinstance(result, dict):
        raise ToolError(f"capability '{tool.name}' returned {type(result).__name__}, expected dict")
    return result


def execute_tool(tool: Tool, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Validate, authorize, then run one capability call.

    Raises :class:`ToolValidationError` before the handler is entered, and
    propagates :class:`NeedsApprovalError` when a live approval is required.
    Execution is delegated to :func:`_invoke_handler` — the one place a tool
    handler can be entered in this codebase.
    """
    errors = validate_arguments(tool.name, tool.parameters, args)
    if errors:
        raise ToolValidationError(tool.name, errors)
    authorize_tool(tool, args, ctx)
    return _invoke_handler(tool, args, ctx)


# ---------------------------------------------------------------------------
# Phase 3: Policy Engine integration — unified policy evaluation + execution
# ---------------------------------------------------------------------------


def _build_policy_context(tool: Tool, args: dict[str, Any], ctx: ToolContext) -> PolicyContext:
    """Build a PolicyContext from tool execution context."""
    return PolicyContext(
        tool_name=tool.name,
        tool=tool,
        arguments=args,
        agent_type=getattr(ctx, "agent_type", None),
        agent_run_id=getattr(ctx, "agent_run_id", None),
        session_id=getattr(ctx, "session_id", None),
        task_id=getattr(ctx, "task_id", None),
        workspace_id=getattr(ctx, "workspace_id", None),
        requester=getattr(ctx, "agent_type", None) or "agent",
        authenticated=bool(getattr(ctx, "session_id", None)),
        permissions=getattr(ctx, "permissions", []),
        workspace_path=getattr(ctx, "workspace_path", "")
        or getattr(ctx, "settings", {}).get("workspaces_dir", ""),
        network_required=getattr(ctx, "network_required", False),
        settings=getattr(ctx, "settings", None),
        factory=getattr(ctx, "factory", None),
        capability_risk=tool.tier,
        capability_scope=tool.scope_for(args),
    )


def execute_with_policy(
    tool: Tool,
    args: dict[str, Any],
    ctx: ToolContext,
    *,
    engine: PolicyEngine | None = None,
) -> tuple[PolicyDecision, dict[str, Any]]:
    """Execute a tool with full policy engine evaluation.

    Returns (policy_decision, tool_output). Raises NeedsApprovalError if
    approval is required, or other ToolError variants on failure.

    This is the Phase 3 unified execution path that evaluates all policy
    dimensions (risk, scope, approval, sandbox, identity, resource, timeout)
    before execution. The engine is the single authorization point here; the
    physical handler invocation is delegated to the canonical run seam
    (:func:`_invoke_handler`), never inlined.
    """
    engine = engine or get_policy_engine(
        factory=getattr(ctx, "factory", None), settings=getattr(ctx, "settings", None)
    )

    # Untrusted model arguments are schema-validated FIRST (INV-002): malformed
    # arguments produce a deterministic ToolValidationError and never reach
    # policy evaluation, scope derivation, an approval record, or the handler.
    errors = validate_arguments(tool.name, tool.parameters, args)
    if errors:
        raise ToolValidationError(tool.name, errors)

    # NORMALIZED ARGUMENTS -> SCOPE DERIVATION -> POLICY -> APPROVAL -> EXECUTION.
    # Policy evaluation happens on arguments that already passed the schema.
    policy_ctx = _build_policy_context(tool, args, ctx)

    # Full policy evaluation; raises NeedsApprovalError when a grant is missing.
    decision, _grant = engine.evaluate_and_authorize(policy_ctx)

    # Delegate execution to the single handler seam. The engine already
    # authorized (and consumed an ALLOW_ONCE grant if one was used), so the
    # gate must not run a second time here.
    return decision, _invoke_handler(tool, args, ctx)


def execute_request_with_policy(
    tool: Tool,
    request: ExecutionRequest,
    ctx: ToolContext,
    *,
    engine: PolicyEngine | None = None,
) -> ExecutionResult:
    """Execute a request with PolicyEngine evaluation, returning ExecutionResult.

    This is the Phase 3 replacement for :func:`execute_request` that uses
    the centralized PolicyEngine for all policy dimensions.
    """
    engine = engine or get_policy_engine(
        factory=getattr(ctx, "factory", None), settings=getattr(ctx, "settings", None)
    )
    metadata = CapabilityMetadata.from_tool(tool, getattr(ctx, "settings", None))

    # Untrusted model arguments are schema-validated FIRST (INV-002). A
    # malformed call is refused deterministically before policy evaluation,
    # scope derivation, an approval record, or the handler can see it.
    errors = validate_arguments(tool.name, tool.parameters, request.arguments)
    if errors:
        exec_decision = ExecutionDecision.invalid(
            metadata, tuple(errors), reason="arguments failed schema validation"
        )
        return ExecutionResult.refuse(
            request,
            exec_decision,
            error=ToolValidationError(tool.name, errors).payload(),
        )

    # NORMALIZED ARGUMENTS -> SCOPE DERIVATION -> POLICY -> APPROVAL -> EXECUTION.
    policy_ctx = _build_policy_context(tool, request.arguments, ctx)

    # Evaluate policy (pure). Durable approval records are materialized so the
    # decision carries a real approval id the caller can decide/notify on.
    decision = engine.evaluate_and_materialize(policy_ctx)

    # Map PolicyDecision to ExecutionDecision for compatibility
    if decision.verdict is PolicyVerdict.DENY:
        exec_decision = ExecutionDecision.deny(
            metadata, "; ".join(decision.denial_reasons), scope=decision.scope
        )
        return ExecutionResult.refuse(request, exec_decision)
    if decision.verdict is PolicyVerdict.AWAIT_APPROVAL:
        exec_decision = ExecutionDecision.await_approval(
            metadata,
            decision.approval.reason,
            scope=decision.scope,
            approval_id=decision.approval.approval_id,
        )
        return ExecutionResult.refuse(request, exec_decision)
    if decision.verdict in (
        PolicyVerdict.INSUFFICIENT_IDENTITY,
        PolicyVerdict.RESOURCE_EXCEEDED,
        PolicyVerdict.TIMEOUT_EXCEEDED,
    ):
        exec_decision = ExecutionDecision.deny(
            metadata, "; ".join(decision.denial_reasons), scope=decision.scope
        )
        return ExecutionResult.refuse(request, exec_decision)
    if decision.verdict is PolicyVerdict.REQUIRES_SANDBOX:
        exec_decision = ExecutionDecision.deny(
            metadata, "sandbox required but unavailable", scope=decision.scope
        )
        return ExecutionResult.refuse(request, exec_decision)

    # Allowed - execute. The engine already authorized, so go straight to the
    # single handler seam rather than re-running the gate (which would
    # double-consume ALLOW_ONCE grants and re-request approvals).
    started = time.perf_counter()
    try:
        output = _invoke_handler(tool, request.arguments, ctx)
    except NeedsApprovalError as exc:
        outcome = ExecutionOutcome.DENY if exc.denied else ExecutionOutcome.AWAIT_APPROVAL
        effective = ExecutionDecision.allow(metadata, "allowed by existing grant").with_outcome(
            outcome, exc.reason or "approval required", approval_id=exc.approval_id
        )
        payload = _permission_error(tool.name, effective, denied=exc.denied)
        return ExecutionResult.refuse(
            request, effective, error=payload, duration_ms=_elapsed_ms(started)
        )
    except ToolValidationError as exc:
        effective = ExecutionDecision.invalid(
            metadata, tuple(exc.errors), reason="arguments failed schema validation"
        )
        return ExecutionResult.refuse(
            request, effective, error=exc.payload(), duration_ms=_elapsed_ms(started)
        )
    except ToolError as exc:
        payload = {"error": "tool_error", "tool": tool.name, "detail": str(exc)}
        return ExecutionResult.failed(
            request,
            ExecutionDecision.allow(metadata, "execution failed"),
            payload,
            duration_ms=_elapsed_ms(started),
        )
    except Exception as exc:
        payload = {
            "error": "capability_crash",
            "tool": tool.name,
            "detail": f"{type(exc).__name__}: {exc}",
        }
        return ExecutionResult.failed(
            request,
            ExecutionDecision.allow(metadata, "execution crashed"),
            payload,
            duration_ms=_elapsed_ms(started),
        )

    if "error" in output:
        return ExecutionResult.failed(
            request,
            ExecutionDecision.allow(metadata, "handler returned error"),
            output,
            duration_ms=_elapsed_ms(started),
        )

    # Success - map policy decision to execution decision
    effective = ExecutionDecision.allow(metadata, "allowed by policy", scope=decision.scope)
    return ExecutionResult.succeeded(request, effective, output, duration_ms=_elapsed_ms(started))


def capability_metadata(tool: Tool, settings: Any = None) -> CapabilityMetadata:
    """Project a registered capability into its contract metadata."""
    return CapabilityMetadata.from_tool(tool, settings)


def decide(tool: Tool, request: ExecutionRequest, settings: Any = None) -> ExecutionDecision:
    """Resolve the declared policy verdict for a request. Pure; never raises.

    Order of evaluation mirrors :func:`execute_tool`: schema validation first,
    then the permission plan. This does *not* consult the live gate (that would
    create approval records as a side effect); a pre-existing grant can still
    let the call run, which is why :func:`execute_request` re-states the
    decision once execution has happened.
    """
    metadata = CapabilityMetadata.from_tool(tool, settings)
    errors = validate_arguments(tool.name, tool.parameters, request.arguments)
    if errors:
        return ExecutionDecision.invalid(metadata, errors)
    plan = plan_permission(tool, request.arguments, settings)
    if plan.default_deny:
        return ExecutionDecision.deny(metadata, plan.reason, scope=plan.scope)
    if plan.requires_approval:
        return ExecutionDecision.await_approval(metadata, plan.reason, scope=plan.scope)
    return ExecutionDecision.allow(metadata, plan.reason, scope=plan.scope)


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _permission_error(tool: str, decision: ExecutionDecision, *, denied: bool) -> dict[str, Any]:
    """The model-readable payload for a refused/paused capability.

    Identical in shape to what the ReAct loop already feeds back, so a result's
    :attr:`ExecutionResult.model_payload` is directly renderable.
    """
    if denied:
        return {
            "error": "permission_denied",
            "tool": tool,
            "approval_id": decision.approval_id,
            "reason": decision.reason,
            "hint": "This capability is refused by policy; choose another approach.",
        }
    return {
        "error": "needs_approval",
        "tool": tool,
        "approval_id": decision.approval_id,
        "hint": "The user must approve this capability, then continue without re-running it.",
    }


def execute_request(tool: Tool, request: ExecutionRequest, ctx: ToolContext) -> ExecutionResult:
    """Execute one request, returning a result value instead of raising.

    Walks the tool lifecycle (``REQUESTED -> VALIDATED -> [AWAITING_APPROVAL] ->
    EXECUTING -> SUCCEEDED|FAILED|REJECTED``) and captures every terminal path:

    - invalid arguments or a default-deny decision -> ``REJECTED`` (no handler);
    - a live approval needed/denied -> ``REJECTED`` (paused, not failed);
    - a handler that raised or returned an ``error`` payload -> ``FAILED``;
    - otherwise -> ``SUCCEEDED``.

    The validate/authorize/run work itself stays in :func:`execute_tool`, so
    there is still exactly one execution seam.
    """
    decision = decide(tool, request, getattr(ctx, "settings", None))
    if decision.outcome is ExecutionOutcome.INVALID:
        payload = ToolValidationError(tool.name, list(decision.errors)).payload()
        return ExecutionResult.refuse(request, decision, error=payload)
    if decision.outcome is ExecutionOutcome.DENY:
        payload = _permission_error(tool.name, decision, denied=True)
        return ExecutionResult.refuse(request, decision, error=payload)

    started = time.perf_counter()
    try:
        output = execute_tool(tool, request.arguments, ctx)
    except NeedsApprovalError as exc:
        outcome = ExecutionOutcome.DENY if exc.denied else ExecutionOutcome.AWAIT_APPROVAL
        effective = decision.with_outcome(
            outcome,
            exc.reason or decision.reason,
            approval_id=exc.approval_id,
        )
        payload = _permission_error(tool.name, effective, denied=exc.denied)
        return ExecutionResult.refuse(
            request, effective, error=payload, duration_ms=_elapsed_ms(started)
        )
    except ToolValidationError as exc:
        # decide() already validated, so this means the schema changed under us.
        effective = decision.with_outcome(
            ExecutionOutcome.INVALID,
            "arguments failed schema validation",
            errors=tuple(exc.errors),
        )
        return ExecutionResult.refuse(
            request, effective, error=exc.payload(), duration_ms=_elapsed_ms(started)
        )
    except ToolError as exc:
        payload = {"error": "tool_error", "tool": tool.name, "detail": str(exc)}
        return ExecutionResult.failed(request, decision, payload, duration_ms=_elapsed_ms(started))
    except Exception as exc:  # a capability crash must not kill the caller
        payload = {
            "error": "capability_crash",
            "tool": tool.name,
            "detail": f"{type(exc).__name__}: {exc}",
        }
        return ExecutionResult.failed(request, decision, payload, duration_ms=_elapsed_ms(started))

    if "error" in output:
        return ExecutionResult.failed(request, decision, output, duration_ms=_elapsed_ms(started))
    # A pre-existing grant can satisfy a plan that said "approval required";
    # the effective decision is the one the live gate produced.
    effective = (
        decision
        if decision.allowed
        else decision.with_outcome(ExecutionOutcome.ALLOW, "allowed by an existing grant")
    )
    return ExecutionResult.succeeded(request, effective, output, duration_ms=_elapsed_ms(started))


# ---------------------------------------------------------------------------
# Unified execution engine — one contract for every tool kind and protocol
# ---------------------------------------------------------------------------


def _placeholder_metadata(name: str) -> CapabilityMetadata:
    """Metadata for a capability the registry does not have.

    Honest rather than invented: origin ``UNKNOWN``, read tier, no approval —
    the refusal itself carries the reason.
    """
    return CapabilityMetadata(
        name=name,
        description="",
        group="unknown",
        capability_risk=CapabilityRisk.READ,
        scope=name,
        requires_approval=False,
        default_deny=False,
        risk_level=classify_risk(CapabilityRisk.READ, name),
        origin=ToolKind.UNKNOWN,
    )


def _refuse_unknown(request: ExecutionRequest, registry: ToolRegistry) -> ExecutionResult:
    """Refuse a call naming a capability the registry does not have."""
    decision = ExecutionDecision.invalid(
        _placeholder_metadata(request.tool),
        [f"unknown capability '{request.tool}'"],
        reason="unknown_capability",
    )
    return ExecutionResult.refuse(
        request,
        decision,
        error={
            "error": "unknown_tool",
            "tool": request.tool,
            "available": registry.names(),
            "hint": "Use one of the available capabilities and call again.",
        },
    )


def _refuse_malformed(
    request: ExecutionRequest,
    parse_error: str,
    tool: Tool | None,
    settings: Any = None,
) -> ExecutionResult:
    """Refuse a call whose arguments never parsed into a JSON object."""
    metadata = (
        CapabilityMetadata.from_tool(tool, settings)
        if tool is not None
        else _placeholder_metadata(request.tool)
    )
    decision = ExecutionDecision.invalid(metadata, [parse_error], reason="malformed_arguments")
    return ExecutionResult.refuse(
        request,
        decision,
        error={
            "error": "malformed_arguments",
            "tool": request.tool,
            "detail": parse_error,
            "hint": "Emit the arguments as a single JSON object and call again.",
        },
    )


def _dispatch_request(
    request: ExecutionRequest, registry: ToolRegistry, ctx: ToolContext
) -> ExecutionResult:
    """Engine core: resolve the request against the registry, then run it."""
    tool = registry.get(request.tool)
    if tool is None:
        return _refuse_unknown(request, registry)
    return execute_request(tool, request, ctx)


def run_tool_call(call: ToolCall, registry: ToolRegistry, ctx: ToolContext) -> ExecutionResult:
    """Unified entry for a parsed model call — any protocol, any tool kind.

    Provider-native calls and Bob-fenced calls both arrive as a ``ToolCall``;
    builtin, MCP, OpenConnector and plugin capabilities are all resolved from
    the same registry. The protocol and the capability origin are recorded as
    provenance on the request and change nothing downstream: every call yields
    the same :class:`ExecutionResult`.
    """
    request = ExecutionRequest.from_tool_call(call, ctx)
    if call.malformed:
        return _refuse_malformed(
            request,
            call.parse_error or "arguments are not valid JSON",
            registry.get(call.name),
            getattr(ctx, "settings", None),
        )
    return _dispatch_request(request, registry, ctx)


def run_request(
    tool_name: str,
    arguments: dict[str, Any] | None,
    registry: ToolRegistry,
    ctx: ToolContext,
    *,
    call_id: str | None = None,
    source: str = "internal",
    protocol: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ExecutionResult:
    """Unified entry for a call that did not come from the model's text.

    Same engine, same contract: only the provenance fields differ.
    """
    request = ExecutionRequest.for_tool(
        tool_name,
        arguments,
        ctx,
        call_id=call_id,
        source=source,
        protocol=protocol,
        metadata=metadata,
    )
    return _dispatch_request(request, registry, ctx)


__all__ = [
    "PermissionPlan",
    "PolicyDecision",
    "PolicyVerdict",
    "authorize_tool",
    "capability_metadata",
    "decide",
    "execute_request",
    "execute_request_with_policy",
    "execute_tool",
    "execute_with_policy",
    "plan_permission",
    "run_request",
    "run_tool_call",
]
