"""Permission & trust system (v3.1 §12–§13).

Centralized Permission Gate. No agent may bypass it. Risk levels map to
policies; every request produces an Approval record with full context.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum

from pydantic import BaseModel, Field

from agent_system.domain.events import utcnow
from agent_system.domain.ids import new_approval_id


class Risk(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Policy(StrEnum):
    ALLOW_ONCE = "ALLOW_ONCE"
    ALLOW_SESSION = "ALLOW_SESSION"
    ALLOW_WORKSPACE = "ALLOW_WORKSPACE"
    ALLOW_ALWAYS = "ALLOW_ALWAYS"
    DENY = "DENY"


class Decision(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"


# Default-deny scopes (v3.1 §13) — cannot be pre-approved via ALLOW_ALWAYS.
DANGEROUS_SCOPES = frozenset(
    {
        "host:filesystem",
        "host:shell",
        "host:credentials",
        "host:users",
        "host:firewall",
        "host:bootloader",
        "host:security_software",
        "browser:transact",
        "credential:transmit",
        "autopilot:input",
        "a2a:delegate",
    }
)

RISK_TTL_MINUTES = {
    Risk.LOW: 60,
    Risk.MEDIUM: 30,
    Risk.HIGH: 15,
    Risk.CRITICAL: 5,
}


class ApprovalRequest(BaseModel):
    requested_action: str
    risk: Risk
    scope: str
    requester: str
    task_id: str | None = None
    agent_run_id: str | None = None
    session_id: str | None = None
    workspace_id: str | None = None
    context: dict[str, object] = Field(default_factory=dict)


class ApprovalRecord(BaseModel):
    approval_id: str
    requested_action: str
    risk: Risk
    scope: str
    requester: str
    task_id: str | None
    agent_run_id: str | None
    session_id: str | None
    workspace_id: str | None
    context: dict[str, object]
    decision: Decision = Decision.PENDING
    decided_by: str | None = None
    reason: str | None = None
    created_at: object = None
    decided_at: object = None
    expires_at: object = None

    def is_expired(self, now: object = None) -> bool:
        from agent_system.domain.events import utcnow as _now

        current = now or _now()
        return self.expires_at is not None and current > self.expires_at  # type: ignore[operator]


class PermissionGate:
    """In-memory approval store; SQLite persistence lands with the API layer.

    Rules enforced here:
    - Dangerous scopes are always default-deny and can never be ALLOW_ALWAYS.
    - Every request yields a full ApprovalRecord with an expiry.
    - Expired approvals deny by default (fail-closed).
    """

    def __init__(self) -> None:
        self._records: dict[str, ApprovalRecord] = {}
        # scope -> list of (policy, record) grants
        self._grants: dict[str, list[tuple[Policy, ApprovalRecord]]] = {}

    def request(self, req: ApprovalRequest) -> ApprovalRecord:
        if req.scope in DANGEROUS_SCOPES:
            record = self._make(
                req, Decision.DENIED, decided_by="gate", reason="default-deny scope"
            )
            return record
        ttl = RISK_TTL_MINUTES[req.risk]
        record = ApprovalRecord(
            approval_id=new_approval_id(),
            requested_action=req.requested_action,
            risk=req.risk,
            scope=req.scope,
            requester=req.requester,
            task_id=req.task_id,
            agent_run_id=req.agent_run_id,
            session_id=req.session_id,
            workspace_id=req.workspace_id,
            context=req.context,
            expires_at=utcnow() + timedelta(minutes=ttl),
        )
        self._records[record.approval_id] = record
        return record

    def decide(
        self,
        approval_id: str,
        approve: bool,
        policy: Policy = Policy.ALLOW_ONCE,
        decided_by: str = "user",
        reason: str | None = None,
    ) -> ApprovalRecord:
        record = self._records.get(approval_id)
        if record is None:
            raise ValueError(f"approval '{approval_id}' not found")
        if record.decision != Decision.PENDING:
            return record
        if record.is_expired():
            record.decision = Decision.EXPIRED
            return record
        if record.scope in DANGEROUS_SCOPES:
            # Cannot approve a default-deny scope through the normal path.
            record.decision = Decision.DENIED
            record.reason = reason or "default-deny scope"
            record.decided_by = decided_by
            record.decided_at = utcnow()
            return record
        if policy == Policy.ALLOW_ALWAYS:
            # Dangerous scopes can never be always-allowed; others may.
            record.decision = Decision.APPROVED
            record.decided_by = decided_by
            record.reason = reason
            record.decided_at = utcnow()
            self._grants.setdefault(record.scope, []).append((policy, record))
        else:
            record.decision = Decision.APPROVED if approve else Decision.DENIED
            record.decided_by = decided_by
            record.reason = reason
            record.decided_at = utcnow()
            if approve:
                self._grants.setdefault(record.scope, []).append((policy, record))
        return record

    def check(self, req: ApprovalRequest) -> tuple[bool, ApprovalRecord | None]:
        """Check an existing grant for scope (+workspace/session when scoped)."""
        for policy, record in reversed(self._grants.get(req.scope, [])):
            if record.is_expired():
                continue
            if policy == Policy.ALLOW_WORKSPACE and record.workspace_id != req.workspace_id:
                continue
            if policy == Policy.ALLOW_SESSION and record.session_id != req.session_id:
                continue
            if policy == Policy.ALLOW_ONCE:
                # consume the grant
                self._grants[req.scope].remove((policy, record))
                return True, record
            return True, record
        return False, None

    def sweep_expired(self) -> list[str]:
        """Mark pending records past expiry as EXPIRED; return their ids."""
        expired: list[str] = []
        now = utcnow()
        for record in self._records.values():
            if record.decision == Decision.PENDING and record.is_expired(now):
                record.decision = Decision.EXPIRED
                expired.append(record.approval_id)
        return expired

    def get(self, approval_id: str) -> ApprovalRecord | None:
        return self._records.get(approval_id)

    def list_all(self) -> list[ApprovalRecord]:
        """Return every approval record (pending + decided)."""
        return list(self._records.values())

    def list_pending(self) -> list[ApprovalRecord]:
        """Public alias for pending() — prefer over private _records access."""
        return self.pending()

    def pending(self) -> list[ApprovalRecord]:
        now = utcnow()
        return [
            r
            for r in self._records.values()
            if r.decision == Decision.PENDING and not r.is_expired(now)
        ]

    def _make(
        self, req: ApprovalRequest, decision: Decision, decided_by: str, reason: str
    ) -> ApprovalRecord:
        record = ApprovalRecord(
            approval_id=new_approval_id(),
            requested_action=req.requested_action,
            risk=req.risk,
            scope=req.scope,
            requester=req.requester,
            task_id=req.task_id,
            agent_run_id=req.agent_run_id,
            session_id=req.session_id,
            workspace_id=req.workspace_id,
            context=req.context,
            decision=decision,
            decided_by=decided_by,
            reason=reason,
            created_at=utcnow(),
            decided_at=utcnow(),
            expires_at=utcnow(),
        )
        self._records[record.approval_id] = record
        return record
