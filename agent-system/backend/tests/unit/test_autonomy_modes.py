"""Autonomy modes gate risk, they never widen default-deny scopes.

The invariant that matters: no mode other than build/auto ever approves a
destructive capability or a DANGEROUS_SCOPES target, and unknown mode strings
fall back to the most restrictive behaviour (build) rather than granting more.
"""

from __future__ import annotations

import pytest

from agent_system.services.permissions import (
    AutonomyMode,
    CapabilityRisk,
    Risk,
    resolve_autonomy_mode,
)
from agent_system.services.tools.execution import plan_permission
from agent_system.services.tools.registry import Tool


class _Settings:
    def __init__(self, autonomy_mode: str = "build", require_approval: bool = True) -> None:
        self.autonomy_mode = autonomy_mode
        self.tools_require_approval = require_approval


def _tool(tier: CapabilityRisk, name: str = "thing", scope: str = "workspace:write") -> Tool:
    return Tool(
        name=name,
        description="test capability",
        parameters={"type": "object", "properties": {}},
        risk=tier,
        handler=lambda args, ctx: {},
        scope=scope,
    )


def test_unknown_mode_falls_back_to_build() -> None:
    assert resolve_autonomy_mode(_Settings("nonsense")) is AutonomyMode.BUILD
    assert resolve_autonomy_mode(_Settings("")) is AutonomyMode.BUILD


def test_override_beats_config() -> None:
    assert resolve_autonomy_mode(_Settings("build"), "auto") is AutonomyMode.AUTO


class TestAutoMode:
    def test_low_risk_runs_unattended(self) -> None:
        plan = plan_permission(_tool(CapabilityRisk.READ), {}, _Settings("auto"))
        assert not plan.requires_approval
        assert not plan.default_deny
        assert "AUTO" in plan.reason

    def test_write_risk_runs_unattended(self) -> None:
        plan = plan_permission(_tool(CapabilityRisk.WRITE), {}, _Settings("auto"))
        assert not plan.requires_approval

    def test_execute_risk_still_asks(self) -> None:
        # EXECUTE classifies HIGH, so autonomy must not silently cover it.
        plan = plan_permission(_tool(CapabilityRisk.EXECUTE), {}, _Settings("auto"))
        assert plan.requires_approval
        assert not plan.default_deny

    def test_destructive_is_default_deny_in_auto(self) -> None:
        plan = plan_permission(_tool(CapabilityRisk.DESTRUCTIVE), {}, _Settings("auto"))
        assert plan.default_deny
        assert plan.requires_approval

    @pytest.mark.parametrize("scope", ["host:filesystem", "host:shell", "credential:transmit"])
    def test_dangerous_scope_default_deny_in_auto(self, scope: str) -> None:
        plan = plan_permission(_tool(CapabilityRisk.READ, scope=scope), {}, _Settings("auto"))
        assert plan.default_deny


class TestPlanMode:
    def test_read_is_allowed(self) -> None:
        plan = plan_permission(_tool(CapabilityRisk.READ), {}, _Settings("plan"))
        assert not plan.requires_approval
        assert not plan.default_deny

    def test_write_is_refused_not_deferred(self) -> None:
        plan = plan_permission(_tool(CapabilityRisk.WRITE), {}, _Settings("plan"))
        assert plan.default_deny
        assert "PLAN" in plan.reason

    def test_execute_is_refused(self) -> None:
        plan = plan_permission(_tool(CapabilityRisk.EXECUTE), {}, _Settings("plan"))
        assert plan.default_deny


class TestUnrestrictedMode:
    def test_skips_approval_but_keeps_destructive_refusal(self) -> None:
        s = _Settings("unrestricted")
        plan = plan_permission(_tool(CapabilityRisk.WRITE), {}, s)
        assert not plan.requires_approval
        # Unrestricted is admin automation, not a licence to run rm -rf.
        assert plan_permission(_tool(CapabilityRisk.DESTRUCTIVE), {}, s).default_deny
        assert plan_permission(
            _tool(CapabilityRisk.READ, scope="host:filesystem"), {}, s
        ).default_deny


class TestBuildModeUnchanged:
    def test_read_needs_no_approval(self) -> None:
        plan = plan_permission(_tool(CapabilityRisk.READ), {}, _Settings("build"))
        assert not plan.requires_approval

    def test_write_follows_tools_require_approval(self) -> None:
        assert plan_permission(
            _tool(CapabilityRisk.WRITE), {}, _Settings("build", True)
        ).requires_approval
        assert not plan_permission(
            _tool(CapabilityRisk.WRITE), {}, _Settings("build", False)
        ).requires_approval


def test_risk_classification_underpins_auto_gate() -> None:
    """AUTO's auto-approval must stop at the tiers that can change the world.

    CAPABILITY_RISK_TO_RISK is read -> LOW, write -> MEDIUM, execute -> HIGH,
    destructive -> CRITICAL, so AUTO covering exactly {LOW, MEDIUM} means
    AUTO grants reads and plain writes but still asks before running anything
    (execute) and never for destructive work. If that mapping ever shifts, this
    test fails instead of autonomy quietly widening.
    """
    from agent_system.services.permissions import AUTO_APPROVED_RISK, CAPABILITY_RISK_TO_RISK

    assert AUTO_APPROVED_RISK == frozenset({Risk.LOW, Risk.MEDIUM})
    assert CAPABILITY_RISK_TO_RISK[CapabilityRisk.READ] is Risk.LOW
    assert CAPABILITY_RISK_TO_RISK[CapabilityRisk.WRITE] is Risk.MEDIUM
    assert CAPABILITY_RISK_TO_RISK[CapabilityRisk.EXECUTE] not in AUTO_APPROVED_RISK
    assert CAPABILITY_RISK_TO_RISK[CapabilityRisk.DESTRUCTIVE] not in AUTO_APPROVED_RISK
