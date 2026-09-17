"""Tool contract — the typed shapes one capability invocation moves through.

The harness freezes *what crosses the execution seam* before attaching any
behaviour to it. Five types are the whole vocabulary:

- :class:`CapabilityMetadata` — what a capability *is* (declaration);
- :class:`ExecutionRequest` — what is being asked for (intent);
- :class:`ExecutionDecision` — what policy says about it (verdict — pure);
- :class:`ToolLifecycle` — where the request is in flight (state machine);
- :class:`ExecutionResult` — what actually happened (outcome).

Nothing in this module executes anything: the single execution path stays in
:mod:`agent_system.services.tools.execution`. These are data contracts, so a
caller can inspect, log, replay or refuse a call without running a handler,
and every stage of a call can be serialized with :meth:`as_dict`.

Semantics that are deliberately fixed here:

* ``decide()`` (execution.py) is **pure** — it reports the verdict the declared
  policy implies and never touches the gate. The *effective* decision after the
  live gate has run is carried by :class:`ExecutionResult`.
* A ``destructive`` tier or a dangerous scope is ``default_deny`` — it can be
  refused but never approved away (see ``services/permissions.py``).
* Unknown capability tiers fail safe to ``execute`` and unknown transitions
  raise :class:`ToolLifecycleError` — never silently coerced.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from agent_system.domain.ids import new_tool_call_id
from agent_system.services.permissions import (
    CapabilityRisk,
    Risk,
    classify_risk,
    is_dangerous_scope,
)
from agent_system.services.tools.protocol import ToolCall
from agent_system.services.tools.registry import Tool, ToolKind

__all__ = [
    "TERMINAL_STATES",
    "TRANSITIONS",
    "CapabilityMetadata",
    "ExecutionDecision",
    "ExecutionOutcome",
    "ExecutionRequest",
    "ExecutionResult",
    "ToolLifecycle",
    "ToolLifecycleError",
    "can_transition",
    "is_terminal",
    "validate_transition",
]


class ToolLifecycle(StrEnum):
    """Where one capability invocation is in flight.

    Values are upper-case to match the other lifecycles in the system
    (:class:`agent_system.domain.tasks.TaskState`,
    :class:`agent_system.domain.lifecycles.AgentState`) — this is a lifecycle,
    not a permission outcome.
    """

    REQUESTED = "REQUESTED"
    VALIDATED = "VALIDATED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    REJECTED = "REJECTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


#: Explicit transition table: state -> set of allowed next states.
TRANSITIONS: dict[ToolLifecycle, frozenset[ToolLifecycle]] = {
    ToolLifecycle.REQUESTED: frozenset({ToolLifecycle.VALIDATED, ToolLifecycle.REJECTED}),
    ToolLifecycle.VALIDATED: frozenset(
        {
            ToolLifecycle.AWAITING_APPROVAL,
            ToolLifecycle.EXECUTING,
            ToolLifecycle.REJECTED,
        }
    ),
    ToolLifecycle.AWAITING_APPROVAL: frozenset({ToolLifecycle.EXECUTING, ToolLifecycle.REJECTED}),
    ToolLifecycle.EXECUTING: frozenset({ToolLifecycle.SUCCEEDED, ToolLifecycle.FAILED}),
    ToolLifecycle.REJECTED: frozenset(),
    ToolLifecycle.SUCCEEDED: frozenset(),
    ToolLifecycle.FAILED: frozenset(),
}

#: States from which no further transition is possible.
TERMINAL_STATES = frozenset({ToolLifecycle.REJECTED, ToolLifecycle.SUCCEEDED, ToolLifecycle.FAILED})


class ToolLifecycleError(ValueError):
    """Raised when a tool-lifecycle transition is not allowed."""

    def __init__(self, current: ToolLifecycle, target: ToolLifecycle) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Invalid tool transition: {current.value} -> {target.value}")


def validate_transition(current: ToolLifecycle, target: ToolLifecycle) -> None:
    """Raise :class:`ToolLifecycleError` if ``current -> target`` is not allowed."""
    if target not in TRANSITIONS[current]:
        raise ToolLifecycleError(current, target)


def can_transition(current: ToolLifecycle, target: ToolLifecycle) -> bool:
    """Non-raising form of :func:`validate_transition`."""
    return target in TRANSITIONS[current]


def is_terminal(state: ToolLifecycle) -> bool:
    """True when no further transition is possible from ``state``."""
    return state in TERMINAL_STATES


class ExecutionOutcome(StrEnum):
    """The verdict of a policy evaluation (lower-case, like ``permissions.Outcome``)."""

    ALLOW = "allow"
    AWAIT_APPROVAL = "await_approval"
    DENY = "deny"
    INVALID = "invalid"


@dataclass(frozen=True)
class CapabilityMetadata:
    """The stable, serializable description of one declared capability.

    A projection of :class:`agent_system.services.tools.registry.Tool` that
    drops the handler and precomputes the derived policy (canonical risk level,
    approval requirement, default-deny). Safe to attach to events, recordings
    and API payloads — it never carries code.
    """

    name: str
    description: str
    group: str
    capability_risk: CapabilityRisk
    scope: str
    requires_approval: bool
    default_deny: bool
    risk_level: Risk
    origin: ToolKind
    destructive_reason: str | None = None
    timeout_seconds: int | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_tool(cls, tool: Tool, settings: Any = None) -> CapabilityMetadata:
        """Project a registered capability into its metadata form.

        ``scope`` is the declared *constant* scope; a callable scope is
        resolved per call, so this reports the capability name and the
        per-call value lives on :class:`ExecutionDecision`.
        """
        tier = tool.tier
        scope = tool.scope if isinstance(tool.scope, str) else tool.name
        return cls(
            name=tool.name,
            description=tool.description,
            group=tool.group,
            capability_risk=tier,
            scope=scope,
            requires_approval=tool.permission_required(settings),
            default_deny=tier is CapabilityRisk.DESTRUCTIVE or is_dangerous_scope(scope),
            risk_level=classify_risk(tier, scope),
            origin=tool.origin,
            destructive_reason=tool.destructive_reason,
            timeout_seconds=tool.timeout_seconds,
            parameters=dict(tool.parameters),
            metadata=dict(tool.metadata),
        )

    def as_dict(self) -> dict[str, Any]:
        """Compact, JSON-serializable view (the argument schema is omitted)."""
        return {
            "name": self.name,
            "description": self.description,
            "group": self.group,
            "origin": self.origin.value,
            "capability_risk": self.capability_risk.value,
            "risk_level": self.risk_level.value,
            "scope": self.scope,
            "requires_approval": self.requires_approval,
            "default_deny": self.default_deny,
            "destructive_reason": self.destructive_reason,
            "timeout_seconds": self.timeout_seconds,
        }


def _ctx_attr(ctx: Any, name: str) -> str | None:
    """Read a string attribute off a duck-typed execution context."""
    value = getattr(ctx, name, None)
    return str(value) if value is not None else None


@dataclass(frozen=True)
class ExecutionRequest:
    """One capability invocation: what is being asked for, and from where."""

    call_id: str
    tool: str
    arguments: dict[str, Any]
    session_id: str | None = None
    task_id: str | None = None
    agent_run_id: str | None = None
    agent_type: str | None = None
    workspace_id: str | None = None
    source: str = "internal"
    protocol: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_tool_call(
        cls,
        call: ToolCall,
        ctx: Any = None,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> ExecutionRequest:
        """Build a request from a parsed model call plus its context."""
        return cls(
            call_id=call.id,
            tool=call.name,
            arguments=dict(call.arguments),
            session_id=_ctx_attr(ctx, "session_id"),
            task_id=_ctx_attr(ctx, "task_id"),
            agent_run_id=_ctx_attr(ctx, "agent_run_id"),
            agent_type=_ctx_attr(ctx, "agent_type"),
            workspace_id=_ctx_attr(ctx, "workspace_id"),
            source=call.source,
            protocol=call.protocol.value,
            metadata=dict(metadata or {}),
        )

    @classmethod
    def for_tool(
        cls,
        tool: str,
        arguments: dict[str, Any] | None = None,
        ctx: Any = None,
        *,
        call_id: str | None = None,
        source: str = "internal",
        protocol: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ExecutionRequest:
        """Build a request for a capability not parsed from model text."""
        return cls(
            call_id=call_id or new_tool_call_id(),
            tool=tool,
            arguments=dict(arguments or {}),
            session_id=_ctx_attr(ctx, "session_id"),
            task_id=_ctx_attr(ctx, "task_id"),
            agent_run_id=_ctx_attr(ctx, "agent_run_id"),
            agent_type=_ctx_attr(ctx, "agent_type"),
            workspace_id=_ctx_attr(ctx, "workspace_id"),
            source=source,
            protocol=protocol,
            metadata=dict(metadata or {}),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool": self.tool,
            "arguments": self.arguments,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "agent_run_id": self.agent_run_id,
            "agent_type": self.agent_type,
            "workspace_id": self.workspace_id,
            "source": self.source,
            "protocol": self.protocol,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class ExecutionDecision:
    """The policy verdict for one request, plus the state it leads to.

    ``capability`` is the metadata the verdict was reached against, so a
    decision is self-describing (risk, scope, approval requirement) without a
    second registry lookup.
    """

    capability: CapabilityMetadata
    outcome: ExecutionOutcome
    reason: str
    scope: str
    approval_id: str | None = None
    errors: tuple[str, ...] = ()

    # -- derived facts ------------------------------------------------------

    @property
    def allowed(self) -> bool:
        """True when the request may proceed straight to execution."""
        return self.outcome is ExecutionOutcome.ALLOW

    @property
    def needs_approval(self) -> bool:
        """True when the request is paused for a human decision."""
        return self.outcome is ExecutionOutcome.AWAIT_APPROVAL

    @property
    def refused(self) -> bool:
        """True when the request will never run as asked (denied or invalid)."""
        return self.outcome in (ExecutionOutcome.DENY, ExecutionOutcome.INVALID)

    @property
    def capability_risk(self) -> CapabilityRisk:
        return self.capability.capability_risk

    @property
    def risk_level(self) -> Risk:
        return self.capability.risk_level

    @property
    def next_state(self) -> ToolLifecycle:
        """The lifecycle state this decision hands the request to."""
        return {
            ExecutionOutcome.ALLOW: ToolLifecycle.EXECUTING,
            ExecutionOutcome.AWAIT_APPROVAL: ToolLifecycle.AWAITING_APPROVAL,
            ExecutionOutcome.DENY: ToolLifecycle.REJECTED,
            ExecutionOutcome.INVALID: ToolLifecycle.REJECTED,
        }[self.outcome]

    # -- construction -------------------------------------------------------

    @classmethod
    def from_metadata(
        cls,
        capability: CapabilityMetadata,
        outcome: ExecutionOutcome,
        reason: str,
        *,
        scope: str | None = None,
        approval_id: str | None = None,
        errors: tuple[str, ...] | list[str] = (),
    ) -> ExecutionDecision:
        return cls(
            capability=capability,
            outcome=outcome,
            reason=reason,
            scope=scope or capability.scope,
            approval_id=approval_id,
            errors=tuple(errors),
        )

    @classmethod
    def allow(
        cls, capability: CapabilityMetadata, reason: str, *, scope: str | None = None
    ) -> ExecutionDecision:
        return cls.from_metadata(capability, ExecutionOutcome.ALLOW, reason, scope=scope)

    @classmethod
    def await_approval(
        cls,
        capability: CapabilityMetadata,
        reason: str,
        *,
        scope: str | None = None,
        approval_id: str | None = None,
    ) -> ExecutionDecision:
        return cls.from_metadata(
            capability,
            ExecutionOutcome.AWAIT_APPROVAL,
            reason,
            scope=scope,
            approval_id=approval_id,
        )

    @classmethod
    def deny(
        cls,
        capability: CapabilityMetadata,
        reason: str,
        *,
        scope: str | None = None,
        approval_id: str | None = None,
    ) -> ExecutionDecision:
        return cls.from_metadata(
            capability,
            ExecutionOutcome.DENY,
            reason,
            scope=scope,
            approval_id=approval_id,
        )

    @classmethod
    def invalid(
        cls,
        capability: CapabilityMetadata,
        errors: tuple[str, ...] | list[str],
        *,
        reason: str = "arguments failed schema validation",
        scope: str | None = None,
    ) -> ExecutionDecision:
        return cls.from_metadata(
            capability,
            ExecutionOutcome.INVALID,
            reason,
            scope=scope,
            errors=tuple(errors),
        )

    def with_outcome(
        self,
        outcome: ExecutionOutcome,
        reason: str,
        *,
        approval_id: str | None = None,
        errors: tuple[str, ...] | None = None,
    ) -> ExecutionDecision:
        """Re-state this verdict after the live gate ran (same capability/scope)."""
        return replace(
            self,
            outcome=outcome,
            reason=reason,
            approval_id=approval_id,
            errors=self.errors if errors is None else tuple(errors),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability.name,
            "origin": self.capability.origin.value,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "scope": self.scope,
            "capability_risk": self.capability_risk.value,
            "risk_level": self.risk_level.value,
            "requires_approval": self.capability.requires_approval,
            "default_deny": self.capability.default_deny,
            "approval_id": self.approval_id,
            "errors": list(self.errors),
            "next_state": self.next_state.value,
        }


@dataclass(frozen=True)
class ExecutionResult:
    """What actually happened to a request, end to end.

    Unlike :func:`agent_system.services.tools.execution.execute_tool` (which
    raises), a result is a value: every terminal path — validated and run,
    refused, paused for approval — is representable, so a caller never needs a
    bare ``except`` to learn the outcome.

    ``output`` carries the handler's dict on success; ``error`` carries a
    model-readable payload otherwise (the same shapes the ReAct loop already
    feeds back to the model, see :meth:`model_payload`).
    """

    request: ExecutionRequest
    lifecycle: ToolLifecycle
    decision: ExecutionDecision
    output: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    duration_ms: float | None = None
    attempts: int = 1

    # -- derived facts ------------------------------------------------------

    @property
    def call_id(self) -> str:
        return self.request.call_id

    @property
    def tool(self) -> str:
        return self.request.tool

    @property
    def ok(self) -> bool:
        """True only when the handler ran and returned a non-error result."""
        return self.lifecycle is ToolLifecycle.SUCCEEDED

    @property
    def refused(self) -> bool:
        """True when the request was denied by policy or rejected as invalid."""
        return self.lifecycle is ToolLifecycle.REJECTED

    @property
    def awaiting_approval(self) -> bool:
        """True when the request is paused, not failed."""
        return self.decision.outcome is ExecutionOutcome.AWAIT_APPROVAL

    @property
    def terminal(self) -> bool:
        return is_terminal(self.lifecycle)

    @property
    def model_payload(self) -> dict[str, Any]:
        """The dict to hand back to the model for this call."""
        if self.ok:
            return self.output
        return self.error or {"error": "unknown", "tool": self.tool}

    # -- construction -------------------------------------------------------

    @classmethod
    def succeeded(
        cls,
        request: ExecutionRequest,
        decision: ExecutionDecision,
        output: dict[str, Any],
        *,
        duration_ms: float | None = None,
        attempts: int = 1,
    ) -> ExecutionResult:
        return cls(
            request=request,
            lifecycle=ToolLifecycle.SUCCEEDED,
            decision=decision,
            output=dict(output),
            duration_ms=duration_ms,
            attempts=attempts,
        )

    @classmethod
    def failed(
        cls,
        request: ExecutionRequest,
        decision: ExecutionDecision,
        error: dict[str, Any],
        *,
        duration_ms: float | None = None,
        attempts: int = 1,
    ) -> ExecutionResult:
        return cls(
            request=request,
            lifecycle=ToolLifecycle.FAILED,
            decision=decision,
            error=dict(error),
            duration_ms=duration_ms,
            attempts=attempts,
        )

    @classmethod
    def refuse(
        cls,
        request: ExecutionRequest,
        decision: ExecutionDecision,
        *,
        error: dict[str, Any] | None = None,
        duration_ms: float | None = None,
    ) -> ExecutionResult:
        return cls(
            request=request,
            lifecycle=ToolLifecycle.REJECTED,
            decision=decision,
            error=dict(error) if error is not None else None,
            duration_ms=duration_ms,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool": self.tool,
            "lifecycle": self.lifecycle.value,
            "ok": self.ok,
            "terminal": self.terminal,
            "decision": self.decision.as_dict(),
            "output": self.output,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "attempts": self.attempts,
            "request": self.request.as_dict(),
        }
