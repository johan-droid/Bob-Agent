"""Centralized Policy Engine (Phase 3).

Single authoritative policy evaluation point that combines:

- Risk classification (LOW/MEDIUM/HIGH/CRITICAL)
- Scope resolution & dangerous scope denial
- Approval gate (PermissionGate)
- Sandbox policy (backend, resource limits, network)
- Identity policy (authentication, agent type, permissions)
- Resource policy (CPU, memory, concurrency, file/output size)
- Timeout policy (execution, approval TTL, idle)

No handler implements its own policy. Every capability invocation flows
through :func:`evaluate_policy` which returns a :class:`PolicyDecision`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from agent_system.config import get_settings
from agent_system.domain.events import utcnow
from agent_system.domain.ids import new_approval_id, new_policy_decision_id
from agent_system.services.permissions import (
    ApprovalRecord,
    ApprovalRequest,
    CapabilityRisk,
    Decision,
    DANGEROUS_SCOPES,
    Outcome,
    PermissionDecision,
    PermissionGate,
    Policy as ApprovalPolicy,
    Risk,
    classify_risk,
    gate_for,
    is_dangerous_scope,
    require_capability,
)
from agent_system.services.sandbox import DockerSandbox, SandboxError, SandboxUnavailableError, SubprocessJail

if TYPE_CHECKING:
    from agent_system.services.tools.registry import Tool, ToolContext, ToolKind


class SandboxBackend(StrEnum):
    """Which sandbox backend to use for execution."""

    DOCKER = "docker"
    SUBPROCESS_JAIL = "subprocess_jail"
    NONE = "none"


class IdentityTier(StrEnum):
    """Identity trust tier (higher = more trusted)."""

    ANONYMOUS = "anonymous"
    SESSION = "session"
    AGENT = "agent"
    ADMIN = "admin"
    SYSTEM = "system"


class PolicyDimension(StrEnum):
    """Individual policy dimensions that contribute to the final decision."""

    RISK = "risk"
    SCOPE = "scope"
    APPROVAL = "approval"
    SANDBOX = "sandbox"
    IDENTITY = "identity"
    RESOURCE = "resource"
    TIMEOUT = "timeout"


class PolicyVerdict(StrEnum):
    """Final policy verdict after evaluating all dimensions."""

    ALLOW = "allow"
    DENY = "deny"
    AWAIT_APPROVAL = "await_approval"
    REQUIRES_SANDBOX = "requires_sandbox"
    INSUFFICIENT_IDENTITY = "insufficient_identity"
    RESOURCE_EXCEEDED = "resource_exceeded"
    TIMEOUT_EXCEEDED = "timeout_exceeded"


@dataclass(frozen=True)
class RiskEvaluation:
    """Risk dimension evaluation result."""

    level: Risk
    capability_tier: CapabilityRisk
    scope: str
    is_dangerous_scope: bool
    reason: str


@dataclass(frozen=True)
class ScopeEvaluation:
    """Scope dimension evaluation result."""

    scope: str
    is_dangerous: bool
    is_default_deny: bool
    reason: str


@dataclass(frozen=True)
class ApprovalEvaluation:
    """Approval dimension evaluation result."""

    outcome: Outcome
    approval_id: str | None
    record: ApprovalRecord | None
    policy: ApprovalPolicy | None
    expires_at: datetime | None
    reason: str


@dataclass(frozen=True)
class SandboxEvaluation:
    """Sandbox dimension evaluation result."""

    required: bool
    backend: SandboxBackend
    image: str | None
    cpu_limit: float
    memory_limit_mb: int
    pids_limit: int
    network_allowed: bool
    timeout_seconds: int
    workspace_path: str
    reason: str


@dataclass(frozen=True)
class IdentityEvaluation:
    """Identity dimension evaluation result."""

    tier: IdentityTier
    authenticated: bool
    agent_type: str | None
    session_id: str | None
    permissions: list[str]
    reason: str


@dataclass(frozen=True)
class ResourceEvaluation:
    """Resource dimension evaluation result."""

    max_concurrent_agents: int
    max_concurrent_tasks: int
    max_workspace_size_mb: int
    max_file_size_mb: int
    max_output_size_mb: int
    max_log_size_mb: int
    max_browser_sessions: int
    max_container_cpu: float
    max_container_memory_mb: int
    current_usage: dict[str, Any] = field(default_factory=dict)
    exceeded: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass(frozen=True)
class TimeoutEvaluation:
    """Timeout dimension evaluation result."""

    execution_timeout_seconds: int
    approval_ttl_minutes: int
    idle_timeout_seconds: int | None
    reason: str


@dataclass(frozen=True)
class PolicyDecision:
    """Complete policy decision across all dimensions.

    This is the single authoritative decision object. Every capability
    invocation receives one of these before execution.
    """

    decision_id: str
    verdict: PolicyVerdict
    tool_name: str
    scope: str
    risk_level: Risk
    capability_tier: CapabilityRisk

    # Per-dimension evaluations
    risk: RiskEvaluation
    scope_eval: ScopeEvaluation
    approval: ApprovalEvaluation
    sandbox: SandboxEvaluation
    identity: IdentityEvaluation
    resource: ResourceEvaluation
    timeout: TimeoutEvaluation

    # Aggregated
    allowed: bool
    requires_approval: bool
    requires_sandbox: bool
    denial_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        """True when the decision is final (no further evaluation needed)."""
        return self.verdict in (
            PolicyVerdict.ALLOW,
            PolicyVerdict.DENY,
            PolicyVerdict.INSUFFICIENT_IDENTITY,
            PolicyVerdict.RESOURCE_EXCEEDED,
            PolicyVerdict.TIMEOUT_EXCEEDED,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "verdict": self.verdict.value,
            "tool_name": self.tool_name,
            "scope": self.scope,
            "risk_level": self.risk_level.value,
            "capability_tier": self.capability_tier.value,
            "allowed": self.allowed,
            "requires_approval": self.requires_approval,
            "requires_sandbox": self.requires_sandbox,
            "denial_reasons": self.denial_reasons,
            "warnings": self.warnings,
            "risk": {
                "level": self.risk.level.value,
                "capability_tier": self.risk.capability_tier.value,
                "scope": self.risk.scope,
                "is_dangerous_scope": self.risk.is_dangerous_scope,
                "reason": self.risk.reason,
            },
            "scope": {
                "scope": self.scope_eval.scope,
                "is_dangerous": self.scope_eval.is_dangerous,
                "is_default_deny": self.scope_eval.is_default_deny,
                "reason": self.scope_eval.reason,
            },
            "approval": {
                "outcome": self.approval.outcome.value,
                "approval_id": self.approval.approval_id,
                "policy": self.approval.policy.value if self.approval.policy else None,
                "expires_at": self.approval.expires_at.isoformat() if self.approval.expires_at else None,
                "reason": self.approval.reason,
            },
            "sandbox": {
                "required": self.sandbox.required,
                "backend": self.sandbox.backend.value,
                "image": self.sandbox.image,
                "cpu_limit": self.sandbox.cpu_limit,
                "memory_limit_mb": self.sandbox.memory_limit_mb,
                "pids_limit": self.sandbox.pids_limit,
                "network_allowed": self.sandbox.network_allowed,
                "timeout_seconds": self.sandbox.timeout_seconds,
                "workspace_path": self.sandbox.workspace_path,
                "reason": self.sandbox.reason,
            },
            "identity": {
                "tier": self.identity.tier.value,
                "authenticated": self.identity.authenticated,
                "agent_type": self.identity.agent_type,
                "session_id": self.identity.session_id,
                "permissions": self.identity.permissions,
                "reason": self.identity.reason,
            },
            "resource": {
                "max_concurrent_agents": self.resource.max_concurrent_agents,
                "max_concurrent_tasks": self.resource.max_concurrent_tasks,
                "max_workspace_size_mb": self.resource.max_workspace_size_mb,
                "max_file_size_mb": self.resource.max_file_size_mb,
                "max_output_size_mb": self.resource.max_output_size_mb,
                "max_log_size_mb": self.resource.max_log_size_mb,
                "max_browser_sessions": self.resource.max_browser_sessions,
                "max_container_cpu": self.resource.max_container_cpu,
                "max_container_memory_mb": self.resource.max_container_memory_mb,
                "current_usage": self.resource.current_usage,
                "exceeded": self.resource.exceeded,
                "reason": self.resource.reason,
            },
            "timeout": {
                "execution_timeout_seconds": self.timeout.execution_timeout_seconds,
                "approval_ttl_minutes": self.timeout.approval_ttl_minutes,
                "idle_timeout_seconds": self.timeout.idle_timeout_seconds,
                "reason": self.timeout.reason,
            },
            "metadata": self.metadata,
        }


class PolicyContext(BaseModel):
    """Unified context for policy evaluation.

    Contains all information needed to evaluate policy across all dimensions.
    """

    # Tool identification
    tool_name: str
    tool: "Tool | None" = None
    arguments: dict[str, Any] = Field(default_factory=dict)

    # Identity
    agent_type: str | None = None
    agent_run_id: str | None = None
    session_id: str | None = None
    task_id: str | None = None
    workspace_id: str | None = None
    requester: str | None = None
    authenticated: bool = False
    permissions: list[str] = Field(default_factory=list)

    # Execution context
    workspace_path: str = ""
    network_required: bool = False

    # Settings (injected for testability)
    settings: Any = None

    # Gate factory (for persistent approvals)
    factory: Any = None

    # Optional: pre-resolved capability metadata
    capability_risk: CapabilityRisk | str | None = None
    capability_scope: str | None = None

    # Additional metadata for resource evaluation, etc.
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(arbitrary_types_allowed=True)


class SandboxProvider(Protocol):
    """Protocol for sandbox backend providers."""

    def run(
        self,
        workspace_path: str,
        command: list[str] | str,
        timeout_seconds: int | None = None,
        network: bool = False,
        image: str | None = None,
    ) -> dict[str, Any]: ...

    def image_present(self, image: str) -> bool: ...

    def ensure_image(self, image: str, dockerfile: Any | None = None) -> bool: ...


class PolicyEngine:
    """Centralized policy evaluation engine.

    Evaluates all policy dimensions and returns a unified PolicyDecision.
    This is the ONLY place policy logic lives — no handler implements
    its own policy checks.
    """

    def __init__(
        self,
        factory: Any = None,
        sandbox_provider: SandboxProvider | None = None,
        settings: Any = None,
    ) -> None:
        self._factory = factory
        self._settings = settings or get_settings()
        self._permission_gate = PermissionGate(factory=factory)
        self._sandbox_provider = sandbox_provider or self._default_sandbox_provider()

    def _default_sandbox_provider(self) -> SandboxProvider | None:
        """Create the default sandbox provider based on settings."""
        if self._settings.heroku_jail:
            return SubprocessJail()
        try:
            return DockerSandbox()
        except SandboxUnavailableError:
            return None

    # ------------------------------------------------------------------------
    # Dimension evaluators
    # ------------------------------------------------------------------------

    def _evaluate_risk(self, ctx: PolicyContext) -> RiskEvaluation:
        """Evaluate risk dimension."""
        tool = ctx.tool
        tier: CapabilityRisk
        if ctx.capability_risk is not None:
            if isinstance(ctx.capability_risk, CapabilityRisk):
                tier = ctx.capability_risk
            else:
                try:
                    tier = CapabilityRisk(ctx.capability_risk)
                except ValueError:
                    tier = CapabilityRisk.EXECUTE
        elif tool is not None:
            tier = tool.tier
        else:
            tier = CapabilityRisk.EXECUTE

        scope = ctx.capability_scope or (tool.scope_for(ctx.arguments) if tool else ctx.tool_name)
        level = classify_risk(tier, scope)
        dangerous = is_dangerous_scope(scope)

        return RiskEvaluation(
            level=level,
            capability_tier=tier,
            scope=scope,
            is_dangerous_scope=dangerous,
            reason=f"tier={tier.value}, scope={scope} -> {level.value}"
            + (" (dangerous scope)" if dangerous else ""),
        )

    def _evaluate_scope(self, ctx: PolicyContext, risk_eval: RiskEvaluation) -> ScopeEvaluation:
        """Evaluate scope dimension."""
        scope = risk_eval.scope
        dangerous = risk_eval.is_dangerous_scope
        default_deny = dangerous or risk_eval.capability_tier is CapabilityRisk.DESTRUCTIVE

        return ScopeEvaluation(
            scope=scope,
            is_dangerous=dangerous,
            is_default_deny=default_deny,
            reason="dangerous scope (default-deny)" if dangerous else "destructive capability (default-deny)"
            if default_deny else "allowed scope",
        )

    def _evaluate_approval(
        self, ctx: PolicyContext, risk_eval: RiskEvaluation, scope_eval: ScopeEvaluation
    ) -> ApprovalEvaluation:
        """Evaluate approval dimension using the PermissionGate."""
        if scope_eval.is_default_deny:
            # Default-deny: create a denied record and return DENY
            req = ApprovalRequest(
                requested_action=f"{ctx.tool_name} {risk_eval.scope}",
                risk=risk_eval.level,
                scope=risk_eval.scope,
                requester=ctx.requester or ctx.agent_type or "agent",
                task_id=ctx.task_id,
                agent_run_id=ctx.agent_run_id,
                session_id=ctx.session_id,
                workspace_id=ctx.workspace_id,
            )
            denied = self._permission_gate.request(req)
            return ApprovalEvaluation(
                outcome=Outcome.DENY,
                approval_id=denied.approval_id,
                record=denied,
                policy=None,
                expires_at=None,
                reason="default-deny scope or destructive capability",
            )

        # Check if approval is required
        tool = ctx.tool
        requires_approval = True
        if tool is not None:
            requires_approval = tool.permission_required(self._settings)
        elif ctx.capability_risk is not None:
            tier = ctx.capability_risk if isinstance(ctx.capability_risk, CapabilityRisk) else CapabilityRisk.EXECUTE
            requires_approval = tier in (CapabilityRisk.WRITE, CapabilityRisk.EXECUTE)

        if not requires_approval:
            return ApprovalEvaluation(
                outcome=Outcome.ALLOW,
                approval_id=None,
                record=None,
                policy=None,
                expires_at=None,
                reason="approval not required for this capability tier",
            )

        # Ask the permission gate
        req = ApprovalRequest(
            requested_action=f"{ctx.tool_name} {risk_eval.scope}",
            risk=risk_eval.level,
            scope=risk_eval.scope,
            requester=ctx.requester or ctx.agent_type or "agent",
            task_id=ctx.task_id,
            agent_run_id=ctx.agent_run_id,
            session_id=ctx.session_id,
            workspace_id=ctx.workspace_id,
        )
        decision = self._permission_gate.authorize(req)

        return ApprovalEvaluation(
            outcome=decision.outcome,
            approval_id=decision.approval_id,
            record=decision.record,
            policy=decision.record.policy if decision.record else None,
            expires_at=decision.record.expires_at if decision.record else None,
            reason=decision.reason or "awaiting approval",
        )

    def _evaluate_sandbox(self, ctx: PolicyContext, risk_eval: RiskEvaluation) -> SandboxEvaluation:
        """Evaluate sandbox dimension."""
        settings = self._settings
        tool = ctx.tool

        # Determine if sandbox is required
        requires_sandbox = False
        if tool is not None:
            requires_sandbox = tool.tier in (CapabilityRisk.EXECUTE, CapabilityRisk.DESTRUCTIVE)
        elif ctx.capability_risk is not None:
            tier = ctx.capability_risk if isinstance(ctx.capability_risk, CapabilityRisk) else CapabilityRisk.EXECUTE
            requires_sandbox = tier in (CapabilityRisk.EXECUTE, CapabilityRisk.DESTRUCTIVE)

        # Shell tools always require sandbox
        if tool and tool.group == "shell":
            requires_sandbox = True

        if not requires_sandbox:
            return SandboxEvaluation(
                required=False,
                backend=SandboxBackend.NONE,
                image=None,
                cpu_limit=0,
                memory_limit_mb=0,
                pids_limit=0,
                network_allowed=False,
                timeout_seconds=0,
                workspace_path=ctx.workspace_path,
                reason="capability does not require sandbox",
            )

        # Determine backend
        if self._settings.heroku_jail:
            backend = SandboxBackend.SUBPROCESS_JAIL
        elif self._sandbox_provider is not None:
            backend = SandboxBackend.DOCKER
        else:
            backend = SandboxBackend.NONE

        # Resolve image
        image = None
        if tool and tool.timeout_seconds:
            pass  # tool-specific timeout handled separately
        if tool and tool.group == "qa":
            image = DockerSandbox.QA_IMAGE

        return SandboxEvaluation(
            required=True,
            backend=backend,
            image=image,
            cpu_limit=settings.max_container_cpu,
            memory_limit_mb=settings.max_container_memory_mb,
            pids_limit=128,
            network_allowed=ctx.network_required,
            timeout_seconds=tool.timeout_seconds if tool and tool.timeout_seconds else settings.max_execution_time_seconds,
            workspace_path=ctx.workspace_path,
            reason=f"tier={risk_eval.capability_tier.value} requires sandbox",
        )

    def _evaluate_identity(self, ctx: PolicyContext) -> IdentityEvaluation:
        """Evaluate identity dimension."""
        tier = IdentityTier.ANONYMOUS
        if ctx.authenticated:
            tier = IdentityTier.SESSION
        if ctx.agent_type:
            tier = IdentityTier.AGENT
        if ctx.permissions and "admin" in ctx.permissions:
            tier = IdentityTier.ADMIN
        if ctx.agent_type == "system":
            tier = IdentityTier.SYSTEM

        return IdentityEvaluation(
            tier=tier,
            authenticated=ctx.authenticated,
            agent_type=ctx.agent_type,
            session_id=ctx.session_id,
            permissions=ctx.permissions,
            reason=f"authenticated={ctx.authenticated}, agent_type={ctx.agent_type}",
        )

    def _evaluate_resource(self, ctx: PolicyContext) -> ResourceEvaluation:
        """Evaluate resource dimension."""
        settings = self._settings
        exceeded = []

        # Check current usage against limits (best-effort, real enforcement is at runtime)
        # These are policy limits; actual enforcement happens in sandbox/execution
        current_usage = ctx.metadata.get("current_usage", {}) if ctx.metadata else {}

        if current_usage.get("concurrent_agents", 0) >= settings.max_concurrent_agents:
            exceeded.append("max_concurrent_agents")
        if current_usage.get("concurrent_tasks", 0) >= settings.max_concurrent_tasks:
            exceeded.append("max_concurrent_tasks")
        if current_usage.get("workspace_size_mb", 0) >= settings.max_workspace_size_mb:
            exceeded.append("max_workspace_size_mb")

        return ResourceEvaluation(
            max_concurrent_agents=settings.max_concurrent_agents,
            max_concurrent_tasks=settings.max_concurrent_tasks,
            max_workspace_size_mb=settings.max_workspace_size_mb,
            max_file_size_mb=settings.max_file_size_mb,
            max_output_size_mb=settings.max_output_size_mb,
            max_log_size_mb=settings.max_log_size_mb,
            max_browser_sessions=settings.max_browser_sessions,
            max_container_cpu=settings.max_container_cpu,
            max_container_memory_mb=settings.max_container_memory_mb,
            current_usage=current_usage,
            exceeded=exceeded,
            reason="resource limits from settings" + (f" exceeded: {exceeded}" if exceeded else ""),
        )

    def _evaluate_timeout(self, ctx: PolicyContext, risk_eval: RiskEvaluation) -> TimeoutEvaluation:
        """Evaluate timeout dimension."""
        settings = self._settings
        tool = ctx.tool

        execution_timeout = settings.max_execution_time_seconds
        if tool and tool.timeout_seconds:
            execution_timeout = min(execution_timeout, tool.timeout_seconds)

        # Approval TTL based on risk level
        from agent_system.services.permissions import RISK_TTL_MINUTES
        approval_ttl = RISK_TTL_MINUTES.get(risk_eval.level, 15)

        return TimeoutEvaluation(
            execution_timeout_seconds=execution_timeout,
            approval_ttl_minutes=approval_ttl,
            idle_timeout_seconds=None,  # Could be configured per-session
            reason=f"risk={risk_eval.level.value} -> approval_ttl={approval_ttl}min, exec_timeout={execution_timeout}s",
        )

    # ------------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------------

    def evaluate(self, ctx: PolicyContext) -> PolicyDecision:
        """Evaluate all policy dimensions and return a unified decision.

        This is the single entry point for policy evaluation. No capability
        handler should implement its own policy logic.
        """
        # Evaluate each dimension
        risk_eval = self._evaluate_risk(ctx)
        scope_eval = self._evaluate_scope(ctx, risk_eval)
        approval_eval = self._evaluate_approval(ctx, risk_eval, scope_eval)
        sandbox_eval = self._evaluate_sandbox(ctx, risk_eval)
        identity_eval = self._evaluate_identity(ctx)
        resource_eval = self._evaluate_resource(ctx)
        timeout_eval = self._evaluate_timeout(ctx, risk_eval)

        # Aggregate verdict
        denial_reasons: list[str] = []
        warnings: list[str] = []

        # Hard denials: default-deny scopes, destructive capabilities, resource limits, identity
        if scope_eval.is_default_deny:
            denial_reasons.append(scope_eval.reason)

        # Resource limits exceeded
        if resource_eval.exceeded:
            denial_reasons.extend([f"resource limit exceeded: {r}" for r in resource_eval.exceeded])

        # Identity check for dangerous operations
        if scope_eval.is_dangerous and identity_eval.tier in (IdentityTier.ANONYMOUS, IdentityTier.SESSION):
            denial_reasons.append(f"dangerous scope requires authenticated agent (current: {identity_eval.tier.value})")

        # Determine final verdict
        if denial_reasons:
            verdict = PolicyVerdict.DENY
            allowed = False
        elif approval_eval.outcome is Outcome.WAIT:
            verdict = PolicyVerdict.AWAIT_APPROVAL
            allowed = False
        elif approval_eval.outcome is Outcome.DENY:
            denial_reasons.append(f"approval denied: {approval_eval.reason}")
            verdict = PolicyVerdict.DENY
            allowed = False
        elif sandbox_eval.required and sandbox_eval.backend is SandboxBackend.NONE:
            verdict = PolicyVerdict.REQUIRES_SANDBOX
            allowed = False
            denial_reasons.append("sandbox required but no backend available")
        else:
            verdict = PolicyVerdict.ALLOW
            allowed = True

        # Warnings
        if sandbox_eval.required and sandbox_eval.backend is SandboxBackend.SUBPROCESS_JAIL:
            warnings.append("running in subprocess jail (no container isolation)")
        if approval_eval.outcome is Outcome.ALLOW and approval_eval.policy == ApprovalPolicy.ALLOW_ONCE:
            warnings.append("approval is single-use (ALLOW_ONCE)")

        return PolicyDecision(
            decision_id=new_policy_decision_id(),
            verdict=verdict,
            tool_name=ctx.tool_name,
            scope=risk_eval.scope,
            risk_level=risk_eval.level,
            capability_tier=risk_eval.capability_tier,
            risk=risk_eval,
            scope_eval=scope_eval,
            approval=approval_eval,
            sandbox=sandbox_eval,
            identity=identity_eval,
            resource=resource_eval,
            timeout=timeout_eval,
            allowed=allowed,
            requires_approval=approval_eval.outcome is Outcome.WAIT,
            requires_sandbox=sandbox_eval.required,
            denial_reasons=denial_reasons,
            warnings=warnings,
            metadata=ctx.metadata or {},
        )

    def evaluate_and_authorize(self, ctx: PolicyContext) -> tuple[PolicyDecision, ApprovalRecord | None]:
        """Evaluate policy and, if allowed, return the granting approval record.

        Returns (decision, grant_record). If decision.allowed is False,
        grant_record is None. Raises NeedsApprovalError if approval is needed.
        """
        from agent_system.services.tool_errors import NeedsApprovalError

        decision = self.evaluate(ctx)

        if not decision.allowed:
            if decision.verdict is PolicyVerdict.AWAIT_APPROVAL:
                raise NeedsApprovalError(
                    approval_id=decision.approval.approval_id or "",
                    action=decision.tool_name,
                    denied=False,
                    reason=decision.approval.reason,
                )
            # For other denials, raise with denied=True
            raise NeedsApprovalError(
                approval_id=decision.approval.approval_id or "",
                action=decision.tool_name,
                denied=True,
                reason="; ".join(decision.denial_reasons) or "policy denied",
            )

        return decision, decision.approval.record

    # ------------------------------------------------------------------------
    # Convenience methods for common operations
    # ------------------------------------------------------------------------

    def check_approval(
        self,
        *,
        scope: str,
        action: str,
        risk: Risk,
        requester: str,
        task_id: str | None = None,
        agent_run_id: str | None = None,
        session_id: str | None = None,
        workspace_id: str | None = None,
    ) -> PermissionDecision:
        """Direct approval check (bypasses other dimensions)."""
        req = ApprovalRequest(
            requested_action=action,
            risk=risk,
            scope=scope,
            requester=requester,
            task_id=task_id,
            agent_run_id=agent_run_id,
            session_id=session_id,
            workspace_id=workspace_id,
        )
        return self._permission_gate.authorize(req)

    def request_approval(
        self,
        *,
        scope: str,
        action: str,
        risk: Risk,
        requester: str,
        task_id: str | None = None,
        agent_run_id: str | None = None,
        session_id: str | None = None,
        workspace_id: str | None = None,
    ) -> ApprovalRecord:
        """Create an approval request."""
        req = ApprovalRequest(
            requested_action=action,
            risk=risk,
            scope=scope,
            requester=requester,
            task_id=task_id,
            agent_run_id=agent_run_id,
            session_id=session_id,
            workspace_id=workspace_id,
        )
        return self._permission_gate.request(req)

    def decide_approval(
        self,
        approval_id: str,
        approve: bool,
        policy: ApprovalPolicy = ApprovalPolicy.ALLOW_ONCE,
        decided_by: str = "user",
        reason: str | None = None,
    ) -> ApprovalRecord:
        """Record an approval decision."""
        return self._permission_gate.decide(approval_id, approve, policy, decided_by, reason)

    def sweep_expired(self) -> list[str]:
        """Mark expired pending approvals as EXPIRED."""
        return self._permission_gate.sweep_expired()

    @property
    def permission_gate(self) -> PermissionGate:
        """Access the underlying permission gate (for advanced use)."""
        return self._permission_gate

    @property
    def sandbox_provider(self) -> SandboxProvider | None:
        """Access the sandbox provider."""
        return self._sandbox_provider


# ---------------------------------------------------------------------------
# Global policy engine instance (lazy initialization)
# ---------------------------------------------------------------------------

_policy_engine: PolicyEngine | None = None


def get_policy_engine(
    factory: Any = None,
    sandbox_provider: SandboxProvider | None = None,
    settings: Any = None,
) -> PolicyEngine:
    """Get or create the global policy engine instance."""
    global _policy_engine
    if _policy_engine is None:
        _policy_engine = PolicyEngine(factory=factory, sandbox_provider=sandbox_provider, settings=settings)
    return _policy_engine


def reset_policy_engine() -> None:
    """Reset the global policy engine (for testing)."""
    global _policy_engine
    _policy_engine = None


def evaluate_policy(ctx: PolicyContext) -> PolicyDecision:
    """Convenience function: evaluate policy using the global engine."""
    return get_policy_engine(factory=ctx.factory, settings=ctx.settings).evaluate(ctx)


def evaluate_and_authorize(ctx: PolicyContext) -> tuple[PolicyDecision, ApprovalRecord | None]:
    """Convenience function: evaluate and authorize using the global engine."""
    return get_policy_engine(factory=ctx.factory, settings=ctx.settings).evaluate_and_authorize(ctx)


__all__ = [
    "ApprovalEvaluation",
    "CapabilityRisk",
    "DANGEROUS_SCOPES",
    "Decision",
    "IdentityEvaluation",
    "IdentityTier",
    "Outcome",
    "PolicyDecision",
    "PolicyDimension",
    "PolicyEngine",
    "PolicyVerdict",
    "ResourceEvaluation",
    "Risk",
    "RiskEvaluation",
    "SandboxBackend",
    "SandboxEvaluation",
    "ScopeEvaluation",
    "TimeoutEvaluation",
    "evaluate_and_authorize",
    "evaluate_policy",
    "get_policy_engine",
    "reset_policy_engine",
]