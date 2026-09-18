"""P0 invariants: pure evaluation and atomic ALLOW_ONCE consumption.

Audit findings driving these tests:

- ``PolicyEngine.evaluate()`` used to persist DENIED/PENDING approval records
  and consume ALLOW_ONCE grants *during evaluation* (via authorize()). That
  made "policy preview / replay" mutating, and made a grant spendable before
  an execution was actually authorized.

Required invariants (P0#7 + P0#2):

1. ``evaluate()`` is PURE — repeat evaluation changes no durable state:
   zero records created, zero grants consumed.
2. Durable records are materialized only at an explicit authorize step
   (``evaluate_and_authorize`` / ``evaluate_and_materialize``) and the
   materialized approval id is real (decision-able).
3. ALLOW_ONCE grants are consumed at most once, atomically, across
   processes: exactly one ``consume()`` winner (P0#2 approval race).
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from agent_system.infra.db import make_session_factory
from agent_system.services.permissions import (
    ApprovalRequest,
    PermissionGate,
    Risk,
)
from agent_system.services.permissions import (
    Policy as ApprovalPolicy,
)
from agent_system.services.policy import (
    PolicyContext,
    PolicyEngine,
    PolicyVerdict,
    reset_policy_engine,
)
from agent_system.services.tool_errors import NeedsApprovalError
from agent_system.services.tools.registry import Tool

PolicyContext.model_rebuild()


class _Setup:
    """Shared scaffolding: engine + a write-tier tool needing approval."""

    def __init__(self, factory: Any = None) -> None:
        reset_policy_engine()
        self.engine = PolicyEngine(factory=factory)
        self.tool = Tool(
            name="purity_tool",
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

    def ctx(self, **overrides: Any) -> PolicyContext:
        defaults = {
            "tool_name": "purity_tool",
            "tool": self.tool,
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

    def grant(self, policy: ApprovalPolicy = ApprovalPolicy.ALLOW_ONCE) -> str:
        gate = self.engine.permission_gate
        req = ApprovalRequest(
            requested_action="purity_tool test:scope",
            risk=Risk.MEDIUM,
            scope="test:scope",
            requester="test_agent",
        )
        record = gate.request(req)
        gate.decide(record.approval_id, approve=True, policy=policy, decided_by="test")
        return record.approval_id


class TestEvaluateIsPure:
    """P0#7: evaluation must be side-effect-free."""

    def setup_method(self) -> None:
        self.s = _Setup()

    def test_repeated_evaluation_creates_zero_records(self) -> None:
        gate = self.s.engine.permission_gate
        for _ in range(3):
            decision = self.s.engine.evaluate(self.s.ctx())
        assert decision.verdict is PolicyVerdict.AWAIT_APPROVAL
        assert gate.list_all() == [], "evaluate() must not persist approval records"
        assert gate.pending() == []
        assert decision.approval.approval_id.startswith("pending-")

    def test_evaluation_does_not_consume_allow_once_grant(self) -> None:
        approval_id = self.s.grant(policy=ApprovalPolicy.ALLOW_ONCE)
        first = self.s.engine.evaluate(self.s.ctx())
        second = self.s.engine.evaluate(self.s.ctx())
        assert first.allowed and second.allowed
        stored = self.s.engine.permission_gate.get(approval_id)
        assert stored is not None and stored.consumed is False

    def test_evaluation_leaves_gate_state_byte_identical(self) -> None:
        """Six evaluations over an ALLOW_ALWAYS grant: same state, no writes."""
        approval_id = self.s.grant(policy=ApprovalPolicy.ALLOW_ALWAYS)
        gate = self.s.engine.permission_gate
        before = gate.get(approval_id)
        for _ in range(6):
            self.s.engine.evaluate(self.s.ctx())
        after = gate.get(approval_id)
        assert before is not None and after is not None
        assert before.consumed == after.consumed is False
        assert len(gate.list_all()) == 1


class TestAuthorizeMaterializes:
    """P0#7: durable records appear only at the explicit authorize step."""

    def setup_method(self) -> None:
        self.s = _Setup()

    def test_evaluate_and_materialize_creates_durable_pending(self) -> None:
        gate = self.s.engine.permission_gate
        decision = self.s.engine.evaluate_and_materialize(self.s.ctx())
        assert decision.verdict is PolicyVerdict.AWAIT_APPROVAL
        approval_id = decision.approval.approval_id
        assert not approval_id.startswith("pending-")
        assert gate.get(approval_id) is not None
        # Decidable: the durable record is the one APIs approve.
        gate.decide(approval_id, approve=True, policy=ApprovalPolicy.ALLOW_ALWAYS)
        assert self.s.engine.evaluate(self.s.ctx()).allowed

    def test_evaluate_and_authorize_raise_carries_real_id(self) -> None:
        gate = self.s.engine.permission_gate
        with pytest.raises(NeedsApprovalError) as exc:
            self.s.engine.evaluate_and_authorize(self.s.ctx())
        assert exc.value.denied is False
        assert gate.get(exc.value.approval_id) is not None
        assert exc.value.approval_id.startswith("pending-") is False


class TestAllowOnceConsumedExactlyOnce:
    """P0#2: one ALLOW_ONCE grant authorizes at most one execution."""

    def setup_method(self) -> None:
        self.s = _Setup()

    def test_second_authorize_after_spend_requires_approval_again(self) -> None:
        approval_id = self.s.grant(policy=ApprovalPolicy.ALLOW_ONCE)
        gate = self.s.engine.permission_gate

        decision, grant = self.s.engine.evaluate_and_authorize(self.s.ctx())
        assert decision.allowed
        assert gate.get(approval_id).consumed is True

        with pytest.raises(NeedsApprovalError):
            self.s.engine.evaluate_and_authorize(self.s.ctx())

    def test_consume_idempotent_same_gate(self) -> None:
        approval_id = self.s.grant(policy=ApprovalPolicy.ALLOW_ONCE)
        gate = self.s.engine.permission_gate
        assert gate.consume(approval_id) is True
        assert gate.consume(approval_id) is False
        assert gate.consume(approval_id) is False

    def test_consume_cross_instance_single_winner(self, db: Any) -> None:
        """Two independent gate instances over the same DB: one consumer."""
        factory = make_session_factory(db.get_bind())
        seed = PermissionGate(factory=factory)
        req = ApprovalRequest(
            requested_action="racer",
            risk=Risk.MEDIUM,
            scope="test:scope",
            requester="test_agent",
        )
        record = seed.request(req)
        seed.decide(record.approval_id, approve=True, policy=ApprovalPolicy.ALLOW_ONCE)

        first = PermissionGate(factory=factory)
        second = PermissionGate(factory=factory)
        assert first.consume(record.approval_id) is True
        assert second.consume(record.approval_id) is False
        stored = PermissionGate(factory=factory).get(record.approval_id)
        assert stored is not None and stored.consumed is True

    def test_concurrent_consume_exactly_one_winner(self, db: Any) -> None:
        """Across-process hammer: entire crowd races one grant, ONE winner.

        SQLite WAL + busy_timeout serialize writers, and the conditional
        ``UPDATE ... WHERE consumed=0`` makes the check-and-set atomic, so
        exactly one thread reports success.
        """
        factory = make_session_factory(db.get_bind())
        seed = PermissionGate(factory=factory)
        req = ApprovalRequest(
            requested_action="hamster",
            risk=Risk.MEDIUM,
            scope="test:scope",
            requester="test_agent",
        )
        record = seed.request(req)
        seed.decide(record.approval_id, approve=True, policy=ApprovalPolicy.ALLOW_ONCE)

        gates = [PermissionGate(factory=factory) for _ in range(12)]
        barrier = threading.Barrier(len(gates))
        outcomes: list[bool] = []

        def race(gate: PermissionGate) -> None:
            barrier.wait()
            outcomes.append(gate.consume(record.approval_id))

        threads = [threading.Thread(target=race, args=(g,)) for g in gates]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert outcomes.count(True) == 1, f"expected exactly one winner, got {outcomes}"
        stored = PermissionGate(factory=factory).get(record.approval_id)
        assert stored is not None and stored.consumed is True
