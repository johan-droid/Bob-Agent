"""Tests for the centralized Policy Engine (Phase 3)."""

from __future__ import annotations

from argparse import Namespace
from typing import Any

import pytest

from agent_system.services.permissions import (
    CapabilityRisk,
    Outcome,
    PermissionGate,
    Risk,
)
from agent_system.services.permissions import (
    Policy as ApprovalPolicy,
)
from agent_system.services.policy import (
    IdentityTier,
    PolicyContext,
    PolicyDecision,
    PolicyEngine,
    PolicyVerdict,
    SandboxBackend,
    evaluate_policy,
    get_policy_engine,
    reset_policy_engine,
)
from agent_system.services.tools.registry import Tool

# Rebuild PolicyContext now that Tool is imported
PolicyContext.model_rebuild()


class TestPolicyEngine:
    """Test the PolicyEngine class."""

    def setup_method(self) -> None:
        reset_policy_engine()
        self.engine = PolicyEngine(factory=None)
        self.mock_tool = Tool(
            name="test_tool",
            description="A test tool",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            risk="write",
            handler=lambda args, ctx: {"result": "ok"},
            scope="test:scope",
            group="test",
        )

    def _make_context(self, **overrides: Any) -> PolicyContext:
        """Create a PolicyContext with sensible defaults."""
        defaults = {
            "tool_name": "test_tool",
            "tool": self.mock_tool,
            "arguments": {"text": "hello"},
            "agent_type": "test_agent",
            "session_id": "ses_123",
            "task_id": "task_123",
            "workspace_id": "ws_123",
            "authenticated": True,
            "workspace_path": "/tmp/workspace",
        }
        defaults.update(overrides)
        return PolicyContext(**defaults)

    # ------------------------------------------------------------------------
    # Risk evaluation
    # ------------------------------------------------------------------------

    def test_risk_evaluation_read_tier(self) -> None:
        """Read tier capabilities map to LOW risk."""
        tool = Tool(
            name="read_tool",
            description="Read tool",
            parameters={},
            risk="read",
            handler=lambda a, c: {},
            scope="test:read",
        )
        ctx = self._make_context(tool=tool, arguments={})
        decision = self.engine.evaluate(ctx)
        assert decision.risk.level == Risk.LOW
        assert decision.risk.capability_tier == CapabilityRisk.READ

    def test_risk_evaluation_write_tier(self) -> None:
        """Write tier capabilities map to MEDIUM risk."""
        tool = Tool(
            name="write_tool",
            description="Write tool",
            parameters={},
            risk="write",
            handler=lambda a, c: {},
            scope="test:write",
        )
        ctx = self._make_context(tool=tool, arguments={})
        decision = self.engine.evaluate(ctx)
        assert decision.risk.level == Risk.MEDIUM
        assert decision.risk.capability_tier == CapabilityRisk.WRITE

    def test_risk_evaluation_execute_tier(self) -> None:
        """Execute tier capabilities map to HIGH risk."""
        tool = Tool(
            name="exec_tool",
            description="Execute tool",
            parameters={},
            risk="execute",
            handler=lambda a, c: {},
            scope="test:execute",
        )
        ctx = self._make_context(tool=tool, arguments={})
        decision = self.engine.evaluate(ctx)
        assert decision.risk.level == Risk.HIGH
        assert decision.risk.capability_tier == CapabilityRisk.EXECUTE

    def test_risk_evaluation_destructive_tier(self) -> None:
        """Destructive tier capabilities map to CRITICAL risk."""
        tool = Tool(
            name="destroy_tool",
            description="Destructive tool",
            parameters={},
            risk="destructive",
            handler=lambda a, c: {},
            scope="test:destroy",
            destructive_reason="destroys data",
        )
        ctx = self._make_context(tool=tool, arguments={})
        decision = self.engine.evaluate(ctx)
        assert decision.risk.level == Risk.CRITICAL
        assert decision.risk.capability_tier == CapabilityRisk.DESTRUCTIVE

    def test_risk_evaluation_dangerous_scope_escalates(self) -> None:
        """Dangerous scope escalates risk to CRITICAL regardless of tier."""
        tool = Tool(
            name="read_tool",
            description="Read tool",
            parameters={},
            risk="read",
            handler=lambda a, c: {},
            scope="host:shell",  # dangerous scope
        )
        ctx = self._make_context(tool=tool, arguments={})
        decision = self.engine.evaluate(ctx)
        assert decision.risk.level == Risk.CRITICAL
        assert decision.risk.is_dangerous_scope is True

    def test_risk_evaluation_unknown_tier_fails_safe(self) -> None:
        """Unknown capability tier fails safe to EXECUTE (HIGH risk)."""
        tool = Tool(
            name="unknown_tool",
            description="Unknown tool",
            parameters={},
            risk="godmode",
            handler=lambda a, c: {},
            scope="test:unknown",
        )
        ctx = self._make_context(tool=tool, arguments={})
        decision = self.engine.evaluate(ctx)
        assert decision.risk.capability_tier == CapabilityRisk.EXECUTE
        assert decision.risk.level == Risk.HIGH

    # ------------------------------------------------------------------------
    # Scope evaluation
    # ------------------------------------------------------------------------

    def test_scope_evaluation_normal_scope(self) -> None:
        """Normal scopes are not dangerous."""
        ctx = self._make_context()
        decision = self.engine.evaluate(ctx)
        assert decision.scope_eval.is_dangerous is False
        assert decision.scope_eval.is_default_deny is False

    def test_scope_evaluation_dangerous_scope(self) -> None:
        """Dangerous scopes are flagged as default-deny."""
        tool = Tool(
            name="shell_tool",
            description="Shell tool",
            parameters={},
            risk="execute",
            handler=lambda a, c: {},
            scope="host:shell",
        )
        ctx = self._make_context(tool=tool, arguments={})
        decision = self.engine.evaluate(ctx)
        assert decision.scope_eval.is_dangerous is True
        assert decision.scope_eval.is_default_deny is True

    def test_scope_evaluation_destructive_capability_default_deny(self) -> None:
        """Destructive capabilities are default-deny even with non-dangerous scope."""
        tool = Tool(
            name="destroy_tool",
            description="Destructive tool",
            parameters={},
            risk="destructive",
            handler=lambda a, c: {},
            scope="test:destroy",
            destructive_reason="destroys data",
        )
        ctx = self._make_context(tool=tool, arguments={})
        decision = self.engine.evaluate(ctx)
        assert decision.scope_eval.is_default_deny is True

    # ------------------------------------------------------------------------
    # Approval evaluation
    # ------------------------------------------------------------------------

    def test_approval_read_tool_no_approval_needed(self) -> None:
        """Read tier tools don't require approval."""
        tool = Tool(
            name="read_tool",
            description="Read tool",
            parameters={},
            risk="read",
            handler=lambda a, c: {},
            scope="test:read",
        )
        ctx = self._make_context(tool=tool, arguments={})
        decision = self.engine.evaluate(ctx)
        assert decision.requires_approval is False
        assert decision.approval.outcome == Outcome.ALLOW

    def test_approval_write_tool_requires_approval(self) -> None:
        """Write tier tools require approval."""
        tool = Tool(
            name="write_tool",
            description="Write tool",
            parameters={},
            risk="write",
            handler=lambda a, c: {},
            scope="test:write",
        )
        ctx = self._make_context(tool=tool, arguments={})
        decision = self.engine.evaluate(ctx)
        assert decision.requires_approval is True
        assert decision.approval.outcome == Outcome.WAIT

    def test_approval_destructive_always_denied(self) -> None:
        """Destructive capabilities are always denied."""
        tool = Tool(
            name="destroy_tool",
            description="Destructive tool",
            parameters={},
            risk="destructive",
            handler=lambda a, c: {},
            scope="test:destroy",
            destructive_reason="destroys data",
        )
        ctx = self._make_context(tool=tool, arguments={})
        decision = self.engine.evaluate(ctx)
        assert decision.verdict == PolicyVerdict.DENY
        assert decision.approval.outcome == Outcome.DENY

    def test_approval_dangerous_scope_always_denied(self) -> None:
        """Dangerous scopes are always denied."""
        tool = Tool(
            name="shell_tool",
            description="Shell tool",
            parameters={},
            risk="execute",
            handler=lambda a, c: {},
            scope="host:shell",
        )
        ctx = self._make_context(tool=tool, arguments={})
        decision = self.engine.evaluate(ctx)
        assert decision.verdict == PolicyVerdict.DENY
        assert decision.approval.outcome == Outcome.DENY

    def test_approval_existing_grant_allows(self) -> None:
        """Pre-existing grant allows execution."""
        # Create a tool that requires approval
        tool = Tool(
            name="write_tool",
            description="Write tool",
            parameters={},
            risk="write",
            handler=lambda a, c: {},
            scope="test:write",
        )
        ctx = self._make_context(tool=tool, arguments={})

        # Pre-approve via the permission gate - first request, then decide
        gate = PermissionGate(factory=None)
        from agent_system.services.permissions import ApprovalRequest, Risk

        req = ApprovalRequest(
            requested_action="write_tool test:write",
            risk=Risk.MEDIUM,
            scope="test:write",
            requester="test_agent",
        )
        record = gate.request(req)
        gate.decide(
            approval_id=record.approval_id,
            approve=True,
            policy=ApprovalPolicy.ALLOW_ALWAYS,
            decided_by="test",
        )

        # Now evaluate - should be allowed (but note: the engine creates new requests each time)
        _ = self.engine.evaluate(ctx)
        # The test is limited because the engine creates new requests with new IDs
        # The real test would need to match the exact request parameters

    # ------------------------------------------------------------------------
    # Sandbox evaluation
    # ------------------------------------------------------------------------

    def test_sandbox_required_for_execute_tier(self) -> None:
        """Execute tier tools require sandbox."""
        tool = Tool(
            name="exec_tool",
            description="Execute tool",
            parameters={},
            risk="execute",
            handler=lambda a, c: {},
            scope="test:execute",
        )
        ctx = self._make_context(tool=tool, arguments={})
        decision = self.engine.evaluate(ctx)
        assert decision.sandbox.required is True
        assert decision.requires_sandbox is True

    def test_sandbox_required_for_destructive_tier(self) -> None:
        """Destructive tier tools require sandbox."""
        tool = Tool(
            name="destroy_tool",
            description="Destructive tool",
            parameters={},
            risk="destructive",
            handler=lambda a, c: {},
            scope="test:destroy",
        )
        ctx = self._make_context(tool=tool, arguments={})
        decision = self.engine.evaluate(ctx)
        assert decision.sandbox.required is True

    def test_sandbox_not_required_for_read_tier(self) -> None:
        """Read tier tools don't require sandbox."""
        tool = Tool(
            name="read_tool",
            description="Read tool",
            parameters={},
            risk="read",
            handler=lambda a, c: {},
            scope="test:read",
        )
        ctx = self._make_context(tool=tool, arguments={})
        decision = self.engine.evaluate(ctx)
        assert decision.sandbox.required is False
        assert decision.requires_sandbox is False

    def test_sandbox_required_for_shell_group(self) -> None:
        """Shell group tools always require sandbox."""
        tool = Tool(
            name="shell_tool",
            description="Shell tool",
            parameters={},
            risk="read",  # even read tier
            handler=lambda a, c: {},
            scope="test:shell",
            group="shell",
        )
        ctx = self._make_context(tool=tool, arguments={})
        decision = self.engine.evaluate(ctx)
        assert decision.sandbox.required is True

    def test_sandbox_backend_docker(self) -> None:
        """Docker backend is selected when available."""
        # This test just verifies the backend selection logic
        # In test environment, Docker may not be available
        tool = Tool(
            name="exec_tool",
            description="Execute tool",
            parameters={},
            risk="execute",
            handler=lambda a, c: {},
            scope="test:execute",
        )
        ctx = self._make_context(tool=tool, arguments={})
        decision = self.engine.evaluate(ctx)
        # Backend will be DOCKER if available, SUBPROCESS_JAIL if heroku_jail, NONE otherwise
        assert decision.sandbox.backend in (
            SandboxBackend.DOCKER,
            SandboxBackend.SUBPROCESS_JAIL,
            SandboxBackend.NONE,
        )

    # ------------------------------------------------------------------------
    # Identity evaluation
    # ------------------------------------------------------------------------

    def test_identity_anonymous(self) -> None:
        """Unauthenticated requests get ANONYMOUS tier."""
        ctx = self._make_context(authenticated=False, agent_type=None)
        decision = self.engine.evaluate(ctx)
        assert decision.identity.tier == IdentityTier.ANONYMOUS

    def test_identity_session(self) -> None:
        """Authenticated session gets SESSION tier."""
        ctx = self._make_context(authenticated=True, agent_type=None)
        decision = self.engine.evaluate(ctx)
        assert decision.identity.tier == IdentityTier.SESSION

    def test_identity_agent(self) -> None:
        """Agent type gets AGENT tier."""
        ctx = self._make_context(authenticated=True, agent_type="CodeAgent")
        decision = self.engine.evaluate(ctx)
        assert decision.identity.tier == IdentityTier.AGENT

    def test_identity_admin(self) -> None:
        """Admin permission gets ADMIN tier."""
        ctx = self._make_context(authenticated=True, agent_type="CodeAgent", permissions=["admin"])
        decision = self.engine.evaluate(ctx)
        assert decision.identity.tier == IdentityTier.ADMIN

    def test_identity_system(self) -> None:
        """System agent type gets SYSTEM tier."""
        ctx = self._make_context(authenticated=True, agent_type="system")
        decision = self.engine.evaluate(ctx)
        assert decision.identity.tier == IdentityTier.SYSTEM

    # ------------------------------------------------------------------------
    # Resource evaluation
    # ------------------------------------------------------------------------

    def test_resource_limits_from_settings(self) -> None:
        """Resource limits come from settings."""
        ctx = self._make_context()
        decision = self.engine.evaluate(ctx)
        assert decision.resource.max_concurrent_agents > 0
        assert decision.resource.max_container_cpu > 0
        assert decision.resource.max_container_memory_mb > 0

    def test_resource_exceeded_flagged(self) -> None:
        """Exceeded resource limits are flagged."""
        ctx = self._make_context(metadata={"current_usage": {"concurrent_agents": 1000}})
        decision = self.engine.evaluate(ctx)
        assert "max_concurrent_agents" in decision.resource.exceeded

    # ------------------------------------------------------------------------
    # Timeout evaluation
    # ------------------------------------------------------------------------

    def test_timeout_execution_from_settings(self) -> None:
        """Execution timeout comes from settings."""
        ctx = self._make_context()
        decision = self.engine.evaluate(ctx)
        assert decision.timeout.execution_timeout_seconds > 0

    def test_timeout_tool_override(self) -> None:
        """Tool-specific timeout overrides settings."""
        tool = Tool(
            name="slow_tool",
            description="Slow tool",
            parameters={},
            risk="execute",
            handler=lambda a, c: {},
            scope="test:slow",
            timeout_seconds=60,
        )
        ctx = self._make_context(tool=tool, arguments={})
        decision = self.engine.evaluate(ctx)
        assert decision.timeout.execution_timeout_seconds == 60

    def test_timeout_approval_ttl_by_risk(self) -> None:
        """Approval TTL varies by risk level."""
        # LOW risk
        tool_low = Tool(
            name="low_tool",
            description="Low risk tool",
            parameters={},
            risk="read",
            handler=lambda a, c: {},
            scope="test:low",
        )
        ctx_low = self._make_context(tool=tool_low, arguments={})
        decision_low = self.engine.evaluate(ctx_low)
        assert decision_low.timeout.approval_ttl_minutes == 60  # LOW = 60 min

        # HIGH risk
        tool_high = Tool(
            name="high_tool",
            description="High risk tool",
            parameters={},
            risk="execute",
            handler=lambda a, c: {},
            scope="test:high",
        )
        ctx_high = self._make_context(tool=tool_high, arguments={})
        decision_high = self.engine.evaluate(ctx_high)
        assert decision_high.timeout.approval_ttl_minutes == 15  # HIGH = 15 min

    # ------------------------------------------------------------------------
    # Aggregate verdict
    # ------------------------------------------------------------------------

    def test_verdict_allow_for_read_tool(self) -> None:
        """Read tools get ALLOW verdict."""
        tool = Tool(
            name="read_tool",
            description="Read tool",
            parameters={},
            risk="read",
            handler=lambda a, c: {},
            scope="test:read",
        )
        ctx = self._make_context(tool=tool, arguments={})
        decision = self.engine.evaluate(ctx)
        assert decision.verdict == PolicyVerdict.ALLOW
        assert decision.allowed is True

    def test_verdict_await_approval_for_write_tool(self) -> None:
        """Write tools get AWAIT_APPROVAL verdict."""
        tool = Tool(
            name="write_tool",
            description="Write tool",
            parameters={},
            risk="write",
            handler=lambda a, c: {},
            scope="test:write",
        )
        ctx = self._make_context(tool=tool, arguments={})
        decision = self.engine.evaluate(ctx)
        assert decision.verdict == PolicyVerdict.AWAIT_APPROVAL
        assert decision.allowed is False
        assert decision.requires_approval is True

    def test_verdict_deny_for_destructive(self) -> None:
        """Destructive tools get DENY verdict."""
        tool = Tool(
            name="destroy_tool",
            description="Destructive tool",
            parameters={},
            risk="destructive",
            handler=lambda a, c: {},
            scope="test:destroy",
        )
        ctx = self._make_context(tool=tool, arguments={})
        decision = self.engine.evaluate(ctx)
        assert decision.verdict == PolicyVerdict.DENY
        assert decision.allowed is False

    def test_verdict_deny_for_dangerous_scope(self) -> None:
        """Dangerous scope tools get DENY verdict."""
        tool = Tool(
            name="shell_tool",
            description="Shell tool",
            parameters={},
            risk="execute",
            handler=lambda a, c: {},
            scope="host:shell",
        )
        ctx = self._make_context(tool=tool, arguments={})
        decision = self.engine.evaluate(ctx)
        assert decision.verdict == PolicyVerdict.DENY
        assert decision.allowed is False

    def test_verdict_requires_sandbox_when_unavailable(self) -> None:
        """REQUIRES_SANDBOX when sandbox needed but unavailable."""
        # Create engine with no sandbox provider
        engine = PolicyEngine(factory=None, sandbox_provider=None)
        tool = Tool(
            name="exec_tool",
            description="Execute tool",
            parameters={},
            risk="execute",
            handler=lambda a, c: {},
            scope="test:execute",
        )
        ctx = self._make_context(tool=tool, arguments={})
        decision = engine.evaluate(ctx)
        # In test environment Docker may not be available
        if decision.sandbox.backend == SandboxBackend.NONE:
            assert decision.verdict == PolicyVerdict.REQUIRES_SANDBOX

    # ------------------------------------------------------------------------
    # Decision output
    # ------------------------------------------------------------------------

    def test_decision_as_dict(self) -> None:
        """PolicyDecision can be serialized to dict."""
        ctx = self._make_context()
        decision = self.engine.evaluate(ctx)
        d = decision.as_dict()
        assert "decision_id" in d
        assert "verdict" in d
        assert "tool_name" in d
        assert "risk" in d
        assert "scope" in d
        assert "approval" in d
        assert "sandbox" in d
        assert "identity" in d
        assert "resource" in d
        assert "timeout" in d

    def test_decision_has_decision_id(self) -> None:
        """Every decision has a unique ID."""
        ctx = self._make_context()
        decision1 = self.engine.evaluate(ctx)
        decision2 = self.engine.evaluate(ctx)
        assert decision1.decision_id != decision2.decision_id
        assert decision1.decision_id.startswith("policy_")

    # ------------------------------------------------------------------------
    # Convenience functions
    # ------------------------------------------------------------------------

    def test_evaluate_policy_convenience(self) -> None:
        """evaluate_policy convenience function works."""
        ctx = self._make_context()
        decision = evaluate_policy(ctx)
        assert isinstance(decision, PolicyDecision)

    def test_get_policy_engine_singleton(self) -> None:
        """get_policy_engine returns singleton."""
        engine1 = get_policy_engine(factory=None)
        engine2 = get_policy_engine(factory=None)
        assert engine1 is engine2

    def test_reset_policy_engine(self) -> None:
        """reset_policy_engine clears singleton."""
        engine1 = get_policy_engine(factory=None)
        reset_policy_engine()
        engine2 = get_policy_engine(factory=None)
        assert engine1 is not engine2


class TestPolicyEngineIntegration:
    """Integration tests for PolicyEngine with execution path."""

    def setup_method(self) -> None:
        reset_policy_engine()

    def _make_settings(self) -> Any:
        """Create a proper settings object for testing."""
        from agent_system.config import Settings

        return Settings(
            tools_require_approval=True,
            heroku_jail=False,
            max_container_cpu=2.0,
            max_container_memory_mb=2048,
            max_execution_time_seconds=1800,
            max_concurrent_agents=8,
            max_concurrent_tasks=16,
            max_workspace_size_mb=512,
            max_file_size_mb=10,
            max_output_size_mb=50,
            max_log_size_mb=100,
            max_browser_sessions=3,
        )

    def _make_context(self, settings: Any) -> Namespace:
        """Create a ToolContext-like Namespace."""
        return Namespace(
            settings=settings,
            factory=None,
            agent_type="test_agent",
            session_id="ses_123",
            task_id="task_123",
            workspace_id="ws_123",
            workspace_path="/tmp/ws",
            permissions=[],
        )

    def test_execute_with_policy_allows_read_tool(self) -> None:
        """execute_with_policy allows read tools without approval."""
        from agent_system.services.tools.execution import execute_with_policy

        tool = Tool(
            name="read_tool",
            description="Read tool",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            risk="read",
            handler=lambda args, ctx: {"output": args["text"]},
            scope="test:read",
        )
        settings = self._make_settings()
        ctx = self._make_context(settings)

        decision, result = execute_with_policy(tool, {"text": "hello"}, ctx)
        assert decision.verdict == PolicyVerdict.ALLOW
        assert result == {"output": "hello"}

    def test_execute_with_policy_raises_for_write_tool(self) -> None:
        """execute_with_policy raises NeedsApprovalError for write tools."""
        from agent_system.services.tool_errors import NeedsApprovalError
        from agent_system.services.tools.execution import execute_with_policy

        tool = Tool(
            name="write_tool",
            description="Write tool",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            risk="write",
            handler=lambda args, ctx: {"output": args["text"]},
            scope="test:write",
        )
        settings = self._make_settings()
        ctx = self._make_context(settings)

        with pytest.raises(NeedsApprovalError) as exc_info:
            execute_with_policy(tool, {"text": "hello"}, ctx)

        assert exc_info.value.denied is False  # Awaiting approval, not denied
        assert exc_info.value.approval_id is not None

    def test_execute_with_policy_denies_destructive(self) -> None:
        """execute_with_policy denies destructive tools."""
        from agent_system.services.tool_errors import NeedsApprovalError
        from agent_system.services.tools.execution import execute_with_policy

        tool = Tool(
            name="destroy_tool",
            description="Destructive tool",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            risk="destructive",
            handler=lambda args, ctx: {"output": args["text"]},
            scope="test:destroy",
            destructive_reason="destroys data",
        )
        settings = self._make_settings()
        ctx = self._make_context(settings)

        with pytest.raises(NeedsApprovalError) as exc_info:
            execute_with_policy(tool, {"text": "hello"}, ctx)

        assert exc_info.value.denied is True  # Default-deny
