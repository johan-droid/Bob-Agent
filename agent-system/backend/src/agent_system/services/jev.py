"""Jev — deterministic decision engine.

The model proposes; Jev disposes. Jev is the deterministic safety layer that
evaluates every significant action before it executes:

    action proposal -> Jev.evaluate() -> ALLOW | ASK | DENY | DELEGATE

Jev does NOT use an LLM. It is pure deterministic logic over:
  - risk classification (LOW/MEDIUM/HIGH/CRITICAL)
  - scope danger (DANGEROUS_SCOPES)
  - autonomy mode (BUILD/PLAN/AUTO/UNRESTRICTED)
  - tool category and target
  - environment (production vs staging vs dev)
  - user role and permissions
  - previous failure history
  - resource sensitivity

This separation is the architectural keystone: the stochastic model never
decides its own permissions. Jev is the one authority.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from agent_system.services.permissions import (
    AutonomyMode,
    Risk,
    is_dangerous_scope,
)

logger = logging.getLogger(__name__)


class JevDecision(StrEnum):
    """The four possible verdicts Jev can return."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"
    DELEGATE = "delegate"


class ToolCategory(StrEnum):
    """Coarse tool categories for risk evaluation."""

    FILESYSTEM = "filesystem"
    SHELL = "shell"
    GIT = "git"
    NETWORK = "network"
    DATABASE = "database"
    BROWSER = "browser"
    MCP = "mcp"
    DEPLOYMENT = "deployment"
    CREDENTIALS = "credentials"
    TELEGRAM = "telegram"
    MEMORY = "memory"
    OTHER = "other"


class Environment(StrEnum):
    """Deployment environment affects risk thresholds."""

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


@dataclass(frozen=True)
class ActionProposal:
    """One proposed action for Jev to evaluate."""

    tool_name: str
    tool_category: ToolCategory
    risk: Risk
    scope: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    environment: Environment = Environment.DEVELOPMENT
    autonomy_mode: AutonomyMode = AutonomyMode.BUILD
    user_role: str = "user"
    target: str = ""
    previous_failures: int = 0
    is_destructive: bool = False
    is_remote: bool = False
    affects_shared_state: bool = False


@dataclass(frozen=True)
class JevVerdict:
    """The result of a Jev evaluation."""

    decision: JevDecision
    reason: str
    risk: Risk
    metadata: dict[str, Any] = field(default_factory=dict)


#: Tool name patterns that are always CRITICAL risk.
_CRITICAL_TOOL_PATTERNS = frozenset(
    {
        "rm",
        "rmdir",
        "drop",
        "truncate",
        "delete",
        "destroy",
        "force_push",
        "force-push",
        "reset_hard",
        "reset --hard",
        "clean",
        "purge",
        "revoke",
        "shutdown",
        "kill",
    }
)

#: Tool name patterns that are always HIGH risk.
_HIGH_TOOL_PATTERNS = frozenset(
    {
        "push",
        "deploy",
        "migrate",
        "alter",
        "modify",
        "update",
        "create",
        "write",
        "edit",
        "insert",
        "remove",
        "install",
        "upgrade",
        "downgrade",
        "restart",
        "reboot",
    }
)

#: Tool categories that are inherently sensitive.
_SENSITIVE_CATEGORIES = frozenset(
    {
        ToolCategory.CREDENTIALS,
        ToolCategory.DEPLOYMENT,
        ToolCategory.DATABASE,
    }
)

#: Scopes that trigger ASK even in AUTO mode.
_ASK_SCOPES = frozenset(
    {
        "git:remote",
        "git:force-push",
        "deploy:production",
        "db:write",
        "db:admin",
        "credentials:read",
        "credentials:write",
        "network:mutation",
    }
)


def _classify_tool_risk(tool_name: str, category: ToolCategory, is_destructive: bool) -> Risk:
    """Determine risk level from tool name, category, and destructiveness."""
    if is_destructive or any(p in tool_name.lower() for p in _CRITICAL_TOOL_PATTERNS):
        return Risk.CRITICAL
    if category in _SENSITIVE_CATEGORIES:
        return Risk.HIGH
    if any(p in tool_name.lower() for p in _HIGH_TOOL_PATTERNS):
        return Risk.HIGH
    if category == ToolCategory.SHELL:
        return Risk.MEDIUM
    return Risk.LOW


def _is_admin_override(user_role: str) -> bool:
    """Admin role can override ASK (but never DENY)."""
    return user_role in {"admin", "system", "owner"}


class Jev:
    """Deterministic decision engine. No LLM calls. Pure logic."""

    def __init__(
        self,
        *,
        environment: Environment = Environment.DEVELOPMENT,
        autonomy_mode: AutonomyMode = AutonomyMode.BUILD,
        strict_mode: bool = False,
    ) -> None:
        self.environment = environment
        self.autonomy_mode = autonomy_mode
        self.strict_mode = strict_mode

    def evaluate(self, proposal: ActionProposal) -> JevVerdict:
        """Evaluate one action proposal and return a verdict.

        The evaluation order matters:
        1. Hard denials (dangerous scopes, destructive in PLAN mode)
        2. Risk-based routing
        3. Autonomy mode gating
        4. Environment escalation
        5. Failure history
        """
        risk = proposal.risk

        # --- Hard denials (never overridable) ---
        if is_dangerous_scope(proposal.scope):
            return JevVerdict(
                decision=JevDecision.DENY,
                reason=f"Scope '{proposal.scope}' is in DANGEROUS_SCOPES — never grantable",
                risk=risk,
                metadata={"denial_type": "dangerous_scope"},
            )

        if self.autonomy_mode == AutonomyMode.PLAN and risk != Risk.LOW:
            return JevVerdict(
                decision=JevDecision.DENY,
                reason="PLAN mode: non-read-only actions are refused",
                risk=risk,
                metadata={"denial_type": "plan_mode"},
            )

        if proposal.is_destructive and self.autonomy_mode != AutonomyMode.UNRESTRICTED:
            return JevVerdict(
                decision=JevDecision.ASK,
                reason="Destructive action requires explicit approval",
                risk=Risk.CRITICAL,
                metadata={"escalation": "destructive"},
            )

        # --- Risk-based routing ---
        if risk == Risk.CRITICAL:
            return JevVerdict(
                decision=JevDecision.ASK,
                reason="CRITICAL risk always requires human approval",
                risk=risk,
                metadata={"escalation": "critical_risk"},
            )

        if risk == Risk.HIGH:
            if self.autonomy_mode == AutonomyMode.UNRESTRICTED and _is_admin_override(
                proposal.user_role
            ):
                return JevVerdict(
                    decision=JevDecision.ALLOW,
                    reason="HIGH risk allowed by admin in UNRESTRICTED mode",
                    risk=risk,
                    metadata={"override": "admin_unrestricted"},
                )
            if proposal.scope in _ASK_SCOPES:
                return JevVerdict(
                    decision=JevDecision.ASK,
                    reason=f"HIGH risk on sensitive scope '{proposal.scope}'",
                    risk=risk,
                    metadata={"escalation": "sensitive_scope"},
                )
            if self.autonomy_mode == AutonomyMode.AUTO and not self.strict_mode:
                return JevVerdict(
                    decision=JevDecision.ALLOW,
                    reason="HIGH risk allowed in AUTO mode (non-strict)",
                    risk=risk,
                    metadata={"auto_approved": True},
                )
            return JevVerdict(
                decision=JevDecision.ASK,
                reason="HIGH risk requires approval",
                risk=risk,
                metadata={"escalation": "high_risk"},
            )

        if risk == Risk.MEDIUM:
            if self.autonomy_mode in {AutonomyMode.AUTO, AutonomyMode.UNRESTRICTED}:
                return JevVerdict(
                    decision=JevDecision.ALLOW,
                    reason=f"MEDIUM risk allowed in {self.autonomy_mode} mode",
                    risk=risk,
                    metadata={"auto_approved": True},
                )
            if self.environment == Environment.PRODUCTION:
                return JevVerdict(
                    decision=JevDecision.ASK,
                    reason="MEDIUM risk in production requires approval",
                    risk=risk,
                    metadata={"escalation": "production"},
                )
            return JevVerdict(
                decision=JevDecision.ALLOW,
                reason="MEDIUM risk in non-production allowed",
                risk=risk,
            )

        # LOW risk
        if self.autonomy_mode == AutonomyMode.BUILD and self.strict_mode:
            return JevVerdict(
                decision=JevDecision.ASK,
                reason="LOW risk but strict mode requires approval",
                risk=risk,
                metadata={"escalation": "strict_mode"},
            )

        # --- Failure history escalation ---
        if proposal.previous_failures >= 3:
            return JevVerdict(
                decision=JevDecision.ASK,
                reason=f"Action failed {proposal.previous_failures} times — escalating to human",
                risk=risk,
                metadata={
                    "escalation": "repeated_failures",
                    "failures": proposal.previous_failures,
                },
            )

        return JevVerdict(
            decision=JevDecision.ALLOW,
            reason="LOW risk, no escalation triggers",
            risk=risk,
        )

    def evaluate_tool_call(
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
        previous_failures: int = 0,
        user_role: str = "user",
    ) -> JevVerdict:
        """Convenience method: evaluate a tool call directly."""
        risk = _classify_tool_risk(tool_name, tool_category, is_destructive)
        proposal = ActionProposal(
            tool_name=tool_name,
            tool_category=tool_category,
            risk=risk,
            scope=scope,
            arguments=arguments or {},
            environment=self.environment,
            autonomy_mode=self.autonomy_mode,
            user_role=user_role,
            target=target,
            previous_failures=previous_failures,
            is_destructive=is_destructive,
            is_remote=is_remote,
            affects_shared_state=affects_shared_state,
        )
        return self.evaluate(proposal)


#: Module-level singleton for simple usage.
_default_jev: Jev | None = None


def get_jev() -> Jev:
    """Get or create the default Jev instance."""
    global _default_jev
    if _default_jev is None:
        _default_jev = Jev()
    return _default_jev


def reset_jev() -> None:
    """Reset the default Jev instance (for tests)."""
    global _default_jev
    _default_jev = None


__all__ = [
    "ActionProposal",
    "Environment",
    "Jev",
    "JevDecision",
    "JevVerdict",
    "ToolCategory",
    "get_jev",
    "reset_jev",
]
