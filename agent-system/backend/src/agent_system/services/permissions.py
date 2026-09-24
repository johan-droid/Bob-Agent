"""Permission & trust system (v3.1 §12–§13).

**One authoritative permission system.** Every capability that executes,
writes, or reaches outside the workspace flows through this module:

    capability request
      -> risk classification        (classify_risk)
      -> policy evaluation          (PermissionGate.check)
      -> existing approval lookup   (durable APPROVED grant, not expired)
      -> approval request if needed (PermissionGate.request -> PENDING)
      -> allow / deny / wait        (PermissionGate.authorize)
      -> tool executor

The gate is the *only* place a capability may learn whether it is allowed to
run. Individual tools must not implement their own approval policy — they call
:func:`require_capability`, which delegates here.

Durability
----------
Records live in SQLite when the gate is constructed with a session ``factory``
(``PermissionGate(factory=...)``). That is the production wiring, and it is why
an approval granted through ``POST /api/v1/approvals/{id}/decision`` is visible
to the tool that requested it, in another process, after a restart. Constructing
a gate without a factory keeps an in-memory store (tests, detached analysis,
the autopilot service) — the semantics are identical, only the lifetime differs.

Risk & policy
-------------
Canonical risk is one of ``LOW``/``MEDIUM``/``HIGH``/``CRITICAL`` and is derived
from a capability's declared risk tier by :func:`classify_risk`. Grants are one
of ``ALLOW_ONCE``/``ALLOW_SESSION``/``ALLOW_WORKSPACE``/``ALLOW_ALWAYS``/``DENY``.
Scopes listed in :data:`DANGEROUS_SCOPES` are hard default-deny: they are never
grantable, regardless of who approves them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, cast

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


class Outcome(StrEnum):
    """Result of a permission evaluation (``authorize``)."""

    ALLOW = "allow"
    DENY = "deny"
    WAIT = "wait"


class CapabilityRisk(StrEnum):
    """Risk tier a capability declares about itself (canonical mapping below)."""

    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    DESTRUCTIVE = "destructive"


#: Canonical capability-tier -> risk-level mapping (v3.1 §13).
CAPABILITY_RISK_TO_RISK: dict[CapabilityRisk, Risk] = {
    CapabilityRisk.READ: Risk.LOW,
    CapabilityRisk.WRITE: Risk.MEDIUM,
    CapabilityRisk.EXECUTE: Risk.HIGH,
    CapabilityRisk.DESTRUCTIVE: Risk.CRITICAL,
}


#: Prefix for records synthesized by :meth:`PermissionGate.peek` during pure
#: evaluation. Such records are NEVER persisted; they only carry request
#: metadata until an explicit :meth:`PolicyEngine` authorize step materializes
#: a real, durable record. The prefix makes "not actually in the store" cheap
#: to detect without a lookup.
EPHEMERAL_APPROVAL_PREFIX = "pending-"


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


def classify_risk(capability_risk: CapabilityRisk | str, scope: str) -> Risk:
    """Map a capability's declared tier (and scope) to a canonical risk level.

    A dangerous scope always classifies CRITICAL — even a "read" tiered
    capability cannot reach a default-deny scope and stay low risk.
    """
    if scope in DANGEROUS_SCOPES:
        return Risk.CRITICAL
    try:
        tier = CapabilityRisk(str(capability_risk))
    except ValueError:
        return Risk.HIGH  # unknown tier fails safe: assume execute-level risk
    return CAPABILITY_RISK_TO_RISK[tier]


def is_dangerous_scope(scope: str) -> bool:
    """True when a scope is default-deny (never grantable)."""
    return scope in DANGEROUS_SCOPES


class ApprovalRequest(BaseModel):
    requested_action: str
    risk: Risk
    scope: str
    requester: str
    task_id: str | None = None
    agent_run_id: str | None = None
    session_id: str | None = None
    workspace_id: str | None = None
    # Server-resolved owner (never client-supplied): set by the REST layer or
    # resolved from the session inside the gate (multi-user isolation §17).
    owner_user_id: str | None = None
    context: dict[str, object] = Field(default_factory=dict)


class ApprovalRecord(BaseModel):
    approval_id: str
    requested_action: str
    risk: Risk
    scope: str
    requester: str
    task_id: str | None = None
    agent_run_id: str | None = None
    session_id: str | None = None
    workspace_id: str | None = None
    context: dict[str, object] = Field(default_factory=dict)
    decision: Decision = Decision.PENDING
    policy: Policy = Policy.ALLOW_ONCE
    # Server-resolved owner (multi-user isolation §17).
    owner_user_id: str | None = None
    consumed: bool = False
    decided_by: str | None = None
    reason: str | None = None
    created_at: datetime | None = None
    decided_at: datetime | None = None
    expires_at: datetime | None = None

    def is_expired(self, now: datetime | None = None) -> bool:
        """True when the record's expiry has passed (fail-closed on compare).

        SQLite returns timezone-naive datetimes, so a value read back from the
        store is normalised to UTC before comparison — otherwise every
        persisted approval would raise on the comparison instead of expiring.
        """
        if self.expires_at is None:
            return False
        current = now or utcnow()
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        return current > expires


class PermissionDecision(BaseModel):
    """What a capability is allowed to do right now."""

    outcome: Outcome
    scope: str
    risk: Risk
    approval_id: str | None = None
    record: ApprovalRecord | None = None
    reason: str | None = None

    @property
    def allowed(self) -> bool:
        return self.outcome is Outcome.ALLOW


class ApprovalStore(Protocol):
    """Persistence seam: in-memory for tests, SQLite for the runtime."""

    def add(self, record: ApprovalRecord) -> None: ...

    def update(self, record: ApprovalRecord) -> None: ...

    def get(self, approval_id: str) -> ApprovalRecord | None: ...

    def list_all(self) -> list[ApprovalRecord]: ...

    def grants_for(self, scope: str) -> list[ApprovalRecord]: ...

    def mark_consumed(self, approval_id: str, now: datetime) -> bool: ...


class MemoryApprovalStore:
    """Process-local store (tests, detached gates). Semantics identical."""

    def __init__(self) -> None:
        self.records: dict[str, ApprovalRecord] = {}

    def add(self, record: ApprovalRecord) -> None:
        self.records[record.approval_id] = record

    def update(self, record: ApprovalRecord) -> None:
        self.records[record.approval_id] = record

    def get(self, approval_id: str) -> ApprovalRecord | None:
        return self.records.get(approval_id)

    def list_all(self) -> list[ApprovalRecord]:
        return list(self.records.values())

    def grants_for(self, scope: str) -> list[ApprovalRecord]:
        return [
            r for r in self.records.values() if r.scope == scope and r.decision is Decision.APPROVED
        ]

    def mark_consumed(self, approval_id: str, now: datetime) -> bool:
        """Atomically consume an ALLOW_ONCE grant. True only for the winner.

        The conditional check-and-set is a single critical section in the
        process-local store, so concurrent ``consume`` calls yield exactly one
        winner — matching the DB-backed store's conditional UPDATE.
        """
        record = self.records.get(approval_id)
        if record is None or record.consumed or record.is_expired(now):
            return False
        record.consumed = True
        return True


def _record_to_row(record: ApprovalRecord) -> Any:
    from agent_system.infra.models import Approval as ApprovalRow

    return ApprovalRow(
        id=record.approval_id,
        task_id=record.task_id,
        agent_run_id=record.agent_run_id,
        session_id=record.session_id,
        workspace_id=record.workspace_id,
        requested_action=record.requested_action,
        risk=record.risk.value,
        scope=record.scope,
        requester=record.requester,
        decision=record.decision.value,
        policy=record.policy.value,
        consumed=record.consumed,
        context_json=dict(record.context),
        decided_by=record.decided_by,
        reason=record.reason,
        owner_user_id=record.owner_user_id,
        created_at=record.created_at or utcnow(),
        decided_at=record.decided_at,
        expires_at=record.expires_at or utcnow(),
    )


def _row_to_record(row: Any) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=row.id,
        requested_action=row.requested_action,
        risk=Risk(row.risk),
        scope=row.scope,
        requester=row.requester,
        task_id=row.task_id,
        agent_run_id=row.agent_run_id,
        session_id=row.session_id,
        workspace_id=row.workspace_id,
        context=dict(row.context_json or {}),
        decision=Decision(row.decision),
        policy=Policy(row.policy or Policy.ALLOW_ONCE.value),
        consumed=bool(row.consumed),
        decided_by=row.decided_by,
        reason=row.reason,
        owner_user_id=row.owner_user_id,
        created_at=row.created_at,
        decided_at=row.decided_at,
        expires_at=row.expires_at,
    )


class DbApprovalStore:
    """SQLite/Postgres-backed store — the authoritative runtime record."""

    def __init__(self, factory: Any) -> None:
        self._factory = factory

    def add(self, record: ApprovalRecord) -> None:
        from agent_system.infra.db import session_scope

        with session_scope(self._factory) as db:
            db.add(_record_to_row(record))

    def update(self, record: ApprovalRecord) -> None:
        from agent_system.infra.db import session_scope
        from agent_system.infra.models import Approval as ApprovalRow

        with session_scope(self._factory) as db:
            row = db.get(ApprovalRow, record.approval_id)
            if row is None:
                db.add(_record_to_row(record))
                return
            row.decision = record.decision.value
            row.policy = record.policy.value
            row.consumed = record.consumed
            row.decided_by = record.decided_by
            row.reason = record.reason
            row.decided_at = record.decided_at
            if record.expires_at is not None:
                row.expires_at = record.expires_at

    def get(self, approval_id: str) -> ApprovalRecord | None:
        from agent_system.infra.db import session_scope
        from agent_system.infra.models import Approval as ApprovalRow

        with session_scope(self._factory) as db:
            row = db.get(ApprovalRow, approval_id)
            return _row_to_record(row) if row is not None else None

    def list_all(self) -> list[ApprovalRecord]:
        from agent_system.infra.db import session_scope
        from agent_system.infra.models import Approval as ApprovalRow

        with session_scope(self._factory) as db:
            rows = db.query(ApprovalRow).order_by(ApprovalRow.created_at.desc()).all()
            return [_row_to_record(r) for r in rows]

    def grants_for(self, scope: str) -> list[ApprovalRecord]:
        from agent_system.infra.db import session_scope
        from agent_system.infra.models import Approval as ApprovalRow

        with session_scope(self._factory) as db:
            rows = (
                db.query(ApprovalRow)
                .filter_by(scope=scope, decision=Decision.APPROVED.value)
                .order_by(ApprovalRow.created_at.desc())
                .all()
            )
            return [_row_to_record(r) for r in rows]

    def mark_consumed(self, approval_id: str, now: datetime) -> bool:
        """Atomically consume an ALLOW_ONCE grant; True only for the winner.

        A single conditional ``UPDATE ... WHERE consumed=0 AND not expired``
        runs under the store's write lock, so concurrent processes racing for
        one grant yield exactly one winner. This is the cross-process guard
        behind :meth:`PermissionGate.consume` (v3.1 §12 ALLOW_ONCE).
        """
        from sqlalchemy import or_
        from sqlalchemy import update as sa_update

        from agent_system.infra.db import session_scope
        from agent_system.infra.models import Approval as ApprovalRow

        with session_scope(self._factory) as db:
            result = db.execute(
                sa_update(ApprovalRow)
                .where(
                    ApprovalRow.id == approval_id,
                    ApprovalRow.consumed.is_(False),
                    or_(ApprovalRow.expires_at.is_(None), ApprovalRow.expires_at > now),
                )
                .values(consumed=True)
            )
            return (cast(Any, result).rowcount or 0) > 0

    def decide_atomic(
        self,
        approval_id: str,
        *,
        decision: str,
        policy: str,
        decided_by: str,
        reason: str | None,
        decided_at: datetime,
    ) -> bool:
        """Conditional UPDATE ... WHERE decision='PENDING'. True iff we won."""
        from sqlalchemy import update as sa_update

        from agent_system.infra.db import session_scope
        from agent_system.infra.models import Approval as ApprovalRow

        with session_scope(self._factory) as db:
            result = db.execute(
                sa_update(ApprovalRow)
                .where(
                    ApprovalRow.id == approval_id,
                    ApprovalRow.decision == Decision.PENDING.value,
                )
                .values(
                    decision=decision,
                    policy=policy,
                    decided_by=decided_by,
                    reason=reason,
                    decided_at=decided_at,
                )
            )
            return (cast(Any, result).rowcount or 0) > 0


class PermissionGate:
    """The one authoritative approval store and evaluator (v3.1 §12–§13).

    Rules enforced here:
    - Dangerous scopes are always default-deny and can never be ALLOW_ALWAYS.
    - Every request yields a full ApprovalRecord with an expiry.
    - Expired approvals deny by default (fail-closed).
    - ALLOW_ONCE grants are consumed by the first successful check; the
      consumption is persisted, so it holds across processes and restarts.
    """

    def __init__(self, factory: Any = None) -> None:
        self._store: ApprovalStore = (
            DbApprovalStore(factory) if factory is not None else MemoryApprovalStore()
        )
        #: Backwards-compatible view of in-memory records. DB-backed gates keep
        #: this empty; call ``get``/``list_all`` for the authoritative view.
        self._records: dict[str, ApprovalRecord] = {}

    # -- requests & decisions ------------------------------------------------

    def request_approval(
        self,
        requested_action: str,
        risk: Risk | str = Risk.HIGH,
        scope: str = "custom",
        requester: str = "agent",
        task_id: str | None = None,
        agent_run_id: str | None = None,
        session_id: str | None = None,
        workspace_id: str | None = None,
        context: dict[str, Any] | None = None,
        owner_user_id: str | None = None,
    ) -> ApprovalRecord:
        req = ApprovalRequest(
            requested_action=requested_action,
            risk=Risk(risk) if isinstance(risk, str) else risk,
            scope=scope,
            requester=requester,
            task_id=task_id,
            agent_run_id=agent_run_id,
            session_id=session_id,
            workspace_id=workspace_id,
            context=context or {},
            owner_user_id=owner_user_id,
        )
        return self.request(req)

    def request(self, req: ApprovalRequest) -> ApprovalRecord:
        """Create an approval request. Dangerous scopes are denied on the spot."""
        if is_dangerous_scope(req.scope):
            return self._make(req, Decision.DENIED, decided_by="gate", reason="default-deny scope")
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
            owner_user_id=req.owner_user_id,
            created_at=utcnow(),
            expires_at=utcnow() + timedelta(minutes=RISK_TTL_MINUTES[req.risk]),
        )
        self._store.add(record)
        self._records[record.approval_id] = record
        return record

    def decide(
        self,
        approval_id: str,
        approve: bool,
        policy: Policy = Policy.ALLOW_ONCE,
        decided_by: str = "user",
        reason: str | None = None,
        decided_by_user_id: str | None = None,
    ) -> ApprovalRecord:
        """Record a user/admin decision. The first decision sticks.

        Multi-user isolation: when ``owner_user_id`` is set on the approval,
        only that owner (or an admin) may decide it. Unknown deciders receive
        a ``ValueError`` so the API layer can return 404/403.
        Concurrency: DB-backed gates use a conditional UPDATE ... WHERE
        decision='PENDING' so concurrent approve/deny yields one winner.
        """
        record = self._store.get(approval_id)
        if record is None:
            raise ValueError(f"approval '{approval_id}' not found")
        # Ownership gate FIRST (P0#3 object isolation): a different identified
        # user must not learn the outcome or contents of an approval they do
        # not own — including already-decided records (the first-decision-sticks
        # early return below would otherwise leak the full record).
        if (
            record.owner_user_id is not None
            and decided_by_user_id is not None
            and record.owner_user_id != decided_by_user_id
        ):
            raise ValueError(f"approval '{approval_id}' belongs to a different owner")
        if record.decision is not Decision.PENDING:
            return record
        if record.is_expired():
            record.decision = Decision.EXPIRED
            self._persist(record)
            return record
        if is_dangerous_scope(record.scope) or policy is Policy.DENY:
            # Cannot approve a default-deny scope, nor approve with DENY.
            new_decision = Decision.DENIED
            new_reason = reason or (
                "default-deny scope" if is_dangerous_scope(record.scope) else "denied"
            )
        else:
            new_decision = Decision.APPROVED if approve else Decision.DENIED
            new_reason = reason
        # Atomic path for DB store: first writer wins, losers get fresh row.
        decide_atomic = getattr(self._store, "decide_atomic", None)
        if callable(decide_atomic):
            decided_at = utcnow()
            decide_atomic(
                approval_id,
                decision=new_decision.value,
                policy=policy.value,
                decided_by=decided_by,
                reason=new_reason,
                decided_at=decided_at,
            )
            fresh = self._store.get(approval_id)
            if fresh is not None:
                self._records[fresh.approval_id] = fresh
                return fresh
            # Fallthrough (row vanished): return in-memory view.
            record.decision = new_decision
            record.policy = policy
            record.decided_by = decided_by
            record.reason = new_reason
            record.decided_at = decided_at
            return record
        record.decision = new_decision
        record.policy = policy
        record.decided_by = decided_by
        record.reason = new_reason
        record.decided_at = utcnow()
        self._persist(record)
        return record

    def check(self, req: ApprovalRequest) -> tuple[bool, ApprovalRecord | None]:
        """Check for a live grant covering this request.

        Honors the grant's policy: ALLOW_ONCE is consumed atomically via
        mark_consumed (one winner under concurrency), ALLOW_SESSION requires
        a matching session, and ALLOW_WORKSPACE requires a matching workspace.
        Owner isolation: grants only match requests from the same owner.
        """
        now = utcnow()
        for record in self._store.grants_for(req.scope):
            if record.is_expired(now) or record.consumed:
                continue
            # Owner isolation: never consume another user's grant.
            if (
                record.owner_user_id is not None
                and req.owner_user_id is not None
                and record.owner_user_id != req.owner_user_id
            ):
                continue
            if record.policy is Policy.ALLOW_SESSION and record.session_id != req.session_id:
                continue
            if record.policy is Policy.ALLOW_WORKSPACE and record.workspace_id != req.workspace_id:
                continue
            if record.policy is Policy.ALLOW_ONCE:
                # Atomic consume: losers skip to next grant.
                try:
                    won = self._store.mark_consumed(record.approval_id, now)
                except Exception:
                    won = False
                if not won:
                    # Re-read: winner already consumed it.
                    continue
                record.consumed = True
            self._records[record.approval_id] = record
            return True, record
        return False, None

    # -- the single evaluation entrypoint ------------------------------------

    def authorize(self, req: ApprovalRequest) -> PermissionDecision:
        """Evaluate a capability request: allow, deny, or wait for approval.

        This is the only function execution paths should call. It never
        executes anything itself and never throws for policy reasons.
        """
        if is_dangerous_scope(req.scope):
            denied = self.request(req)  # persists the DENIED audit record
            return PermissionDecision(
                outcome=Outcome.DENY,
                scope=req.scope,
                risk=denied.risk,
                approval_id=denied.approval_id,
                record=denied,
                reason="default-deny scope",
            )
        allowed, granted = self.check(req)
        if allowed and granted is not None:
            return PermissionDecision(
                outcome=Outcome.ALLOW,
                scope=req.scope,
                risk=granted.risk,
                approval_id=granted.approval_id,
                record=granted,
                reason="existing grant",
            )
        pending = self.request(req)
        if pending.decision is Decision.DENIED:
            return PermissionDecision(
                outcome=Outcome.DENY,
                scope=req.scope,
                risk=pending.risk,
                approval_id=pending.approval_id,
                record=pending,
                reason=pending.reason,
            )
        return PermissionDecision(
            outcome=Outcome.WAIT,
            scope=req.scope,
            risk=pending.risk,
            approval_id=pending.approval_id,
            record=pending,
            reason="awaiting approval",
        )

    def peek(self, req: ApprovalRequest) -> PermissionDecision:
        """Pure, read-only evaluation — same verdict logic as :meth:`authorize`.

        P0 invariant: evaluation must be side-effect-free so policy can be
        re-run freely (previews, previews, idempotency and replay) without
        mutating durable state. ``peek`` creates NO records, consumes NO
        grants, and writes NOTHING. Result records that do not exist in the
        store are synthesized with an ``pending-*`` id (see
        ``EPHEMERAL_APPROVAL_PREFIX``); materialize them with
        :meth:`consume`/an explicit authorize step only when executing.
        """
        if is_dangerous_scope(req.scope):
            denied = self._build(
                req, Decision.DENIED, decided_by="gate", reason="default-deny scope"
            )
            denied.approval_id = f"{EPHEMERAL_APPROVAL_PREFIX}{denied.approval_id}"
            return PermissionDecision(
                outcome=Outcome.DENY,
                scope=req.scope,
                risk=denied.risk,
                approval_id=denied.approval_id,
                record=denied,
                reason="default-deny scope",
            )
        now = utcnow()
        for record in self._store.grants_for(req.scope):
            if record.is_expired(now) or record.consumed:
                continue
            if record.policy is Policy.ALLOW_SESSION and record.session_id != req.session_id:
                continue
            if record.policy is Policy.ALLOW_WORKSPACE and record.workspace_id != req.workspace_id:
                continue
            return PermissionDecision(
                outcome=Outcome.ALLOW,
                scope=req.scope,
                risk=record.risk,
                approval_id=record.approval_id,
                record=record,
                reason="existing grant",
            )
        pending = self._build(req, Decision.PENDING, decided_by=None, reason=None)
        pending.approval_id = f"{EPHEMERAL_APPROVAL_PREFIX}{pending.approval_id}"
        return PermissionDecision(
            outcome=Outcome.WAIT,
            scope=req.scope,
            risk=pending.risk,
            approval_id=pending.approval_id,
            record=pending,
            reason="awaiting approval",
        )

    def consume(self, approval_id: str) -> bool:
        """Atomically consume an ALLOW_ONCE grant. True only for the winner.

        Idempotent for the winner, a defensive no-op for everyone else:
        already-consumed or expired grants return False. This is the only
        place an ALLOW_ONCE grant is spent, and it is called at execution
        time — never during evaluation.
        """
        spent = self._store.mark_consumed(approval_id, utcnow())
        stored = self._store.get(approval_id)
        if stored is not None:
            # Keep the process-local view coherent with the store.
            self._records[stored.approval_id] = stored
        return spent

    # -- lifecycle ----------------------------------------------------------

    def sweep_expired(self) -> list[str]:
        """Mark pending records past expiry as EXPIRED; return their ids."""
        expired: list[str] = []
        for record in self._store.list_all():
            if record.decision is Decision.PENDING and record.is_expired():
                record.decision = Decision.EXPIRED
                self._persist(record)
                expired.append(record.approval_id)
        return expired

    def get(self, approval_id: str) -> ApprovalRecord | None:
        record = self._store.get(approval_id)
        if record is not None:
            self._records[record.approval_id] = record
        return record

    def list_all(self) -> list[ApprovalRecord]:
        """Every approval record (pending + decided)."""
        records = self._store.list_all()
        for record in records:
            self._records[record.approval_id] = record
        return records

    def list_pending(self) -> list[ApprovalRecord]:
        """Public alias for pending() — prefer over private _records access."""
        return self.pending()

    def pending(self) -> list[ApprovalRecord]:
        now = utcnow()
        return [
            r
            for r in self._store.list_all()
            if r.decision is Decision.PENDING and not r.is_expired(now)
        ]

    # -- internals ----------------------------------------------------------

    def _persist(self, record: ApprovalRecord) -> None:
        self._store.update(record)
        self._records[record.approval_id] = record

    def _build(
        self, req: ApprovalRequest, decision: Decision, decided_by: str | None, reason: str | None
    ) -> ApprovalRecord:
        """Pure record constructor — no store writes (used by :meth:`peek`)."""
        return ApprovalRecord(
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
            owner_user_id=req.owner_user_id,
            decision=decision,
            decided_by=decided_by,
            reason=reason,
            created_at=utcnow(),
            decided_at=utcnow(),
            expires_at=utcnow(),
        )

    def _make(
        self, req: ApprovalRequest, decision: Decision, decided_by: str | None, reason: str | None
    ) -> ApprovalRecord:
        record = self._build(req, decision, decided_by, reason)
        self._store.add(record)
        self._records[record.approval_id] = record
        return record


# ---------------------------------------------------------------------------
# Tool-facing seam: the ONE way a capability asks for permission
# ---------------------------------------------------------------------------


def gate_for(ctx: Any) -> PermissionGate:
    """Resolve the gate a tool context must use (never a tool-local policy)."""
    gate = getattr(ctx, "gate", None)
    if isinstance(gate, PermissionGate):
        return gate
    return PermissionGate(factory=getattr(ctx, "factory", None))


def require_capability(
    ctx: Any,
    *,
    scope: str,
    action: str,
    capability_risk: CapabilityRisk | str = CapabilityRisk.EXECUTE,
    requester: str | None = None,
) -> ApprovalRecord | None:
    """Authorize one capability invocation, or raise ``NeedsApprovalError``.

    Returns the granting record on ALLOW (may be ``None`` when the deployment
    has approval requirements switched off), raises on DENY and WAIT. The risk
    level is derived here — callers declare a *tier*, never a level, so the
    canonical mapping cannot drift per tool.
    """
    from agent_system.services.tool_errors import NeedsApprovalError

    settings = getattr(ctx, "settings", None)
    required = bool(getattr(settings, "tools_require_approval", True))
    risk_level = classify_risk(capability_risk, scope)
    # Default-deny is a property of the *capability tier* as well as of the
    # scope: a destructive capability is refused even when approvals are
    # switched off entirely, and even when its scope is not on the
    # DANGEROUS_SCOPES list.
    dangerous_scope = is_dangerous_scope(scope)
    destructive = str(capability_risk) == CapabilityRisk.DESTRUCTIVE.value
    if dangerous_scope or destructive:
        reason = (
            "default-deny scope" if dangerous_scope else "destructive capability (default-deny)"
        )
        raise NeedsApprovalError(
            _deny_record_id(gate_for(ctx), scope, action, risk_level, requester, ctx),
            action,
            denied=True,
            reason=reason,
        )
    if not required:
        return None
    decision = gate_for(ctx).authorize(
        ApprovalRequest(
            requested_action=action,
            risk=risk_level,
            scope=scope,
            requester=requester or str(getattr(ctx, "agent_type", None) or "agent"),
            task_id=getattr(ctx, "task_id", None),
            agent_run_id=getattr(ctx, "agent_run_id", None),
            session_id=getattr(ctx, "session_id", None),
            workspace_id=getattr(ctx, "workspace_id", None),
            context={"capability_risk": str(capability_risk)},
            owner_user_id=getattr(ctx, "owner_user_id", None),
        )
    )
    if decision.outcome is Outcome.ALLOW:
        _emit_decision(ctx, decision)
        return decision.record
    _emit_decision(ctx, decision)
    if decision.outcome is Outcome.DENY:
        raise NeedsApprovalError(
            decision.approval_id or "",
            action,
            denied=True,
            reason=decision.reason,
        )
    raise NeedsApprovalError(decision.approval_id or "", action)


def _deny_record_id(
    gate: PermissionGate,
    scope: str,
    action: str,
    risk: Risk,
    requester: str | None,
    ctx: Any,
) -> str:
    """Persist (once) the DENIED audit record for a default-deny scope."""
    decision = gate.authorize(
        ApprovalRequest(
            requested_action=action,
            risk=risk,
            scope=scope,
            requester=requester or str(getattr(ctx, "agent_type", None) or "agent"),
            task_id=getattr(ctx, "task_id", None),
            agent_run_id=getattr(ctx, "agent_run_id", None),
            session_id=getattr(ctx, "session_id", None),
            workspace_id=getattr(ctx, "workspace_id", None),
            owner_user_id=getattr(ctx, "owner_user_id", None),
        )
    )
    return decision.approval_id or ""


def _emit_decision(ctx: Any, decision: PermissionDecision) -> None:
    """Emit the canonical approval event for a gate decision (best effort)."""
    emit = getattr(ctx, "emit", None)
    if emit is None:
        return
    event_type = {
        Outcome.ALLOW: "approval.approved",
        Outcome.DENY: "approval.denied",
        Outcome.WAIT: "approval.requested",
    }[decision.outcome]
    try:
        emit(
            event_type,
            {
                "approval_id": decision.approval_id,
                "scope": decision.scope,
                "risk": decision.risk.value,
                "outcome": decision.outcome.value,
            },
        )
    except Exception:  # audit emission must never break execution
        pass


__all__ = [
    "CAPABILITY_RISK_TO_RISK",
    "DANGEROUS_SCOPES",
    "RISK_TTL_MINUTES",
    "ApprovalRecord",
    "ApprovalRequest",
    "ApprovalStore",
    "CapabilityRisk",
    "DbApprovalStore",
    "Decision",
    "MemoryApprovalStore",
    "Outcome",
    "PermissionDecision",
    "PermissionGate",
    "Policy",
    "Risk",
    "classify_risk",
    "gate_for",
    "is_dangerous_scope",
    "require_capability",
]
