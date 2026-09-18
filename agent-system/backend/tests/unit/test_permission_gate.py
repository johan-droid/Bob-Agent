"""Unit tests — Permission Gate (v3.1 §12–§13)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from agent_system.domain.events import utcnow
from agent_system.services.permissions import (
    DANGEROUS_SCOPES,
    ApprovalRequest,
    Decision,
    PermissionGate,
    Policy,
    Risk,
)


def _req(**kwargs: object) -> ApprovalRequest:
    defaults: dict[str, object] = {
        "requested_action": "fs.write",
        "risk": Risk.MEDIUM,
        "scope": "file:write",
        "requester": "CodeAgent",
        "session_id": "ses_1",
        "workspace_id": "ws_1",
    }
    return ApprovalRequest(**{**defaults, **kwargs})  # type: ignore[arg-type]


def test_request_creates_pending_record_with_expiry() -> None:
    gate = PermissionGate()
    record = gate.request(_req())
    assert record.decision == Decision.PENDING
    assert record.approval_id.startswith("approval_")
    assert record.expires_at is not None


def test_approve_once_then_consume() -> None:
    gate = PermissionGate()
    record = gate.request(_req())
    gate.decide(record.approval_id, approve=True, policy=Policy.ALLOW_ONCE)
    ok, _ = gate.check(_req())
    assert ok
    # ALLOW_ONCE consumed:
    ok2, _ = gate.check(_req())
    assert not ok2


def test_deny() -> None:
    gate = PermissionGate()
    record = gate.request(_req())
    gate.decide(record.approval_id, approve=False)
    ok, _ = gate.check(_req())
    assert not ok
    assert gate.get(record.approval_id).decision == Decision.DENIED  # type: ignore[union-attr]


def test_allow_session_scoped_to_session() -> None:
    gate = PermissionGate()
    record = gate.request(_req())
    gate.decide(record.approval_id, approve=True, policy=Policy.ALLOW_SESSION)
    assert gate.check(_req(session_id="ses_1"))[0]
    assert not gate.check(_req(session_id="ses_other"))[0]


def test_allow_workspace_scoped_to_workspace() -> None:
    gate = PermissionGate()
    record = gate.request(_req())
    gate.decide(record.approval_id, approve=True, policy=Policy.ALLOW_WORKSPACE)
    assert gate.check(_req(workspace_id="ws_1"))[0]
    assert not gate.check(_req(workspace_id="ws_2"))[0]


def test_dangerous_scopes_default_deny_and_unapprovable() -> None:
    gate = PermissionGate()
    for scope in ("browser:transact", "host:shell", "credential:transmit"):
        assert scope in DANGEROUS_SCOPES
        record = gate.request(_req(scope=scope, risk=Risk.CRITICAL))
        assert record.decision == Decision.DENIED
        # Even a user "approval" cannot rescue it:
        record2 = gate.request(_req(scope=scope, risk=Risk.CRITICAL))
        if record2.decision == Decision.PENDING:  # defensive: gate may auto-deny
            decided = gate.decide(record2.approval_id, approve=True)
            assert decided.decision != Decision.APPROVED or decided.scope not in DANGEROUS_SCOPES


def test_expired_approval_fails_closed() -> None:
    gate = PermissionGate()
    record = gate.request(_req(risk=Risk.LOW))
    gate.decide(record.approval_id, approve=True, policy=Policy.ALLOW_SESSION)
    # Force expiry:
    record.expires_at = utcnow() - timedelta(seconds=1)
    ok, _ = gate.check(_req())
    assert not ok


def test_sweep_expired_marks_expirations() -> None:
    gate = PermissionGate()
    record = gate.request(_req(risk=Risk.CRITICAL))
    record.expires_at = utcnow() - timedelta(seconds=1)
    expired = gate.sweep_expired()
    assert record.approval_id in expired
    assert gate.get(record.approval_id).decision == Decision.EXPIRED  # type: ignore[union-attr]


def test_cannot_decide_twice() -> None:
    gate = PermissionGate()
    record = gate.request(_req())
    gate.decide(record.approval_id, approve=True)
    again = gate.decide(record.approval_id, approve=False)
    assert again.decision == Decision.APPROVED  # first decision sticks


@pytest.mark.parametrize("risk", list(Risk))
def test_all_risks_get_ttl(risk: Risk) -> None:
    gate = PermissionGate()
    record = gate.request(_req(risk=risk))
    assert record.expires_at is not None

    # -- regression: owner identity propagation & cross-owner isolation --

    def test_owner_propagated_through_request() -> None:
        gate = PermissionGate()
        record = gate.request(_req(owner_user_id="user_alice"))
        assert record.owner_user_id == "user_alice"
        persisted = gate.get(record.approval_id)
        assert persisted is not None
        assert persisted.owner_user_id == "user_alice"

    def test_none_decider_allows_when_owner_set() -> None:
        """Backward compat: when no decider identity is supplied the ownership
        gate is skipped so local/legacy paths that do not carry a principal
        can still decide approvals."""
        gate = PermissionGate()
        record = gate.request(_req(owner_user_id="user_alice"))
        decided = gate.decide(record.approval_id, approve=True, decided_by_user_id=None)
        assert decided.decision == Decision.APPROVED

    def test_cross_owner_cannot_decide_approval() -> None:
        gate = PermissionGate()
        record = gate.request(_req(owner_user_id="user_alice"))
        with pytest.raises(ValueError, match="belongs to a different owner"):
            gate.decide(record.approval_id, approve=True, decided_by_user_id="user_bob")

    def test_owner_can_decide_own_approval() -> None:
        gate = PermissionGate()
        record = gate.request(_req(owner_user_id="user_alice"))
        decided = gate.decide(record.approval_id, approve=True, decided_by_user_id="user_alice")
        assert decided.decision == Decision.APPROVED
        assert decided.decided_by == "user"
