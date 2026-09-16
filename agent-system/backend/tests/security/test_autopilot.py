"""Phase 18 — Autopilot security tests (v3.1 §27; off by default, gated).

These tests assert the Phase 18 acceptance criteria:
disabled by default · cannot run without approvals · full audit trail ·
kill-switch works.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_system.services.autopilot import (
    ALLOWED_ACTIONS,
    FORBIDDEN_ACTIONS,
    MAX_ACTIONS_PER_RUN,
    AutopilotAction,
    AutopilotDisabledError,
    AutopilotKilledError,
    AutopilotService,
)
from agent_system.services.permissions import PermissionGate


@pytest.fixture()
def gate() -> PermissionGate:
    return PermissionGate()


@pytest.fixture()
def executor_calls() -> list[tuple[str, dict[str, Any]]]:
    return []


@pytest.fixture()
def executor(executor_calls: list[tuple[str, dict[str, Any]]]) -> Any:
    def _exec(action: str, params: dict[str, Any]) -> dict[str, Any]:
        executor_calls.append((action, params))
        return {"ok": True}

    return _exec


def _approved(gate: PermissionGate, action: str, target: str = "button") -> str:
    svc_helper = AutopilotService(gate, enabled=True)
    record = svc_helper.request_action_approval(AutopilotAction(action=action, target=target))
    # Operator approves through the gate (as the API would):
    return gate.decide(record.approval_id, approve=True).approval_id


class TestOffByDefault:
    def test_disabled_by_default(self, gate: PermissionGate) -> None:
        assert AutopilotService(gate).is_enabled is False

    def test_disabled_service_refuses_run(self, gate: PermissionGate) -> None:
        svc = AutopilotService(gate)
        with pytest.raises(AutopilotDisabledError, match="disabled by default"):
            svc.run([(AutopilotAction(action="click"), "approval_x")])
        assert any(e["outcome"] == "refused_disabled" for e in svc.audit())

    def test_killed_service_refuses_run(self, gate: PermissionGate) -> None:
        svc = AutopilotService(gate, enabled=True)
        svc.kill()
        with pytest.raises(AutopilotKilledError, match="kill switch"):
            svc.run([(AutopilotAction(action="click"), "approval_x")])

    def test_kill_switch_blocks_enable(self, gate: PermissionGate) -> None:
        svc = AutopilotService(gate, enabled=False)
        svc.kill()
        with pytest.raises(AutopilotKilledError):
            svc.enable()

    def test_kill_is_sticky_until_reset(self, gate: PermissionGate, executor: Any) -> None:
        svc = AutopilotService(gate, executor=executor, enabled=True)
        approval = _approved(gate, "click")
        svc.kill()
        with pytest.raises(AutopilotKilledError):
            svc.run([(AutopilotAction(action="click"), approval)])
        with pytest.raises(AutopilotKilledError):
            svc.enable()
        svc.reset()
        svc.enable()
        assert svc.is_enabled is True
        result = svc.run([(AutopilotAction(action="click", target="btn"), approval)])
        assert result["executed"] == 1


class TestApprovalBoundary:
    def test_run_without_any_approval_denied(self, gate: PermissionGate, executor: Any) -> None:
        svc = AutopilotService(gate, executor=executor, enabled=True)
        result = svc.run([(AutopilotAction(action="click", target="btn"), "approval_missing")])
        assert result["executed"] == 0
        assert result["results"][0]["outcome"] == "denied_gate"

    def test_pending_approval_denied(
        self, gate: PermissionGate, executor: Any, executor_calls: list[Any]
    ) -> None:
        svc = AutopilotService(gate, executor=executor, enabled=True)
        record = svc.request_action_approval(AutopilotAction(action="type_text"))
        result = svc.run([(AutopilotAction(action="type_text"), record.approval_id)])
        assert result["results"][0]["outcome"] == "denied_gate"
        assert executor_calls == []

    def test_expired_approval_denied(self, gate: PermissionGate, executor: Any) -> None:
        svc = AutopilotService(gate, executor=executor, enabled=True)
        record = svc.request_action_approval(AutopilotAction(action="key_press"))
        gate.decide(record.approval_id, approve=True)
        # Force expiry past the CRITICAL 5-minute TTL:
        gate._records[record.approval_id].expires_at = __import__(
            "agent_system.domain.events", fromlist=["utcnow"]
        ).utcnow() - __import__("datetime").timedelta(seconds=1)
        result = svc.run([(AutopilotAction(action="key_press"), record.approval_id)])
        assert result["results"][0]["outcome"] == "denied_gate"

    def test_approval_bound_to_different_action_denied(
        self, gate: PermissionGate, executor: Any
    ) -> None:
        svc = AutopilotService(gate, executor=executor, enabled=True)
        other = svc.request_action_approval(AutopilotAction(action="scroll"))
        gate.decide(other.approval_id, approve=True)
        result = svc.run([(AutopilotAction(action="click"), other.approval_id)])
        assert result["results"][0]["outcome"] == "denied_gate"

    def test_fresh_bound_approval_executes(
        self, gate: PermissionGate, executor: Any, executor_calls: list[Any]
    ) -> None:
        svc = AutopilotService(gate, executor=executor, enabled=True)
        approval = _approved(gate, "click")
        result = svc.run([(AutopilotAction(action="click", target="OK button"), approval)])
        assert result["executed"] == 1
        assert result["results"][0]["outcome"] == "executed"
        assert executor_calls == [("click", {})]


class TestDefaultDeny:
    def test_forbidden_actions_never_execute(
        self, gate: PermissionGate, executor: Any, executor_calls: list[Any]
    ) -> None:
        svc = AutopilotService(gate, executor=executor, enabled=True)
        for bad in FORBIDDEN_ACTIONS:
            approval = _approved(gate, "click")
            result = svc.run([(AutopilotAction(action=bad), approval)])
            assert result["results"][0]["outcome"] == "forbidden"
        assert executor_calls == []

    def test_unknown_action_forbidden(self, gate: PermissionGate, executor: Any) -> None:
        svc = AutopilotService(gate, executor=executor, enabled=True)
        approval = _approved(gate, "click")
        result = svc.run([(AutopilotAction(action="format_disk"), approval)])
        assert result["results"][0]["outcome"] == "forbidden"

    def test_allowed_action_set_is_narrow(self) -> None:
        assert set(ALLOWED_ACTIONS) == {
            "click",
            "type_text",
            "key_press",
            "scroll",
            "move_mouse",
        }
        assert MAX_ACTIONS_PER_RUN == 50


class TestCapsAndAudit:
    def test_action_cap_stops_run(self, gate: PermissionGate, executor: Any) -> None:
        svc = AutopilotService(gate, executor=executor, enabled=True, max_actions=2)
        pairs = [(AutopilotAction(action="click"), _approved(gate, "click")) for _ in range(5)]
        result = svc.run(pairs)
        assert result["executed"] == 2
        assert any(r["outcome"] == "cap_reached" for r in result["results"])

    def test_audit_trail_complete(self, gate: PermissionGate, executor: Any) -> None:
        svc = AutopilotService(gate, executor=executor, enabled=True)
        approval = _approved(gate, "click")
        svc.run([(AutopilotAction(action="click", target="btn"), approval)])
        svc.run([(AutopilotAction(action="click"), "approval_none")])
        events = svc.audit()
        outcomes = {e["outcome"] for e in events}
        assert "executed" in outcomes
        assert "denied_gate" in outcomes
        # Every entry has a timestamp and monotonically increasing seq
        seqs = [e["seq"] for e in events]
        assert seqs == sorted(seqs)

    def test_kill_switch_appears_in_audit(self, gate: PermissionGate) -> None:
        svc = AutopilotService(gate)
        svc.kill()
        assert any(e["action"] == "kill_switch" for e in svc.audit())

    def test_missing_executor_reports_failure(self, gate: PermissionGate) -> None:
        svc = AutopilotService(gate, executor=None, enabled=True)
        approval = _approved(gate, "click")
        result = svc.run([(AutopilotAction(action="click"), approval)])
        assert result["results"][0]["outcome"] == "no_executor"
        assert result["executed"] == 0
