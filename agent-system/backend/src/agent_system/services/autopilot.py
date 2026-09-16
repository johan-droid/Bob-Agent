"""Autopilot — desktop automation (v3.1 Phase 18, §27 security note).

The most dangerous capability in the system, built last and gated hardest:

- **Off by default**: a no-op unless explicitly enabled. The disabled service
  refuses every run and records the refusal.
- **Own permission boundary**: every input action needs a FRESH, unexpired,
  APPROVED approval record bound to exactly that action (`autopilot:<action>`).
  Expired / pending / mismatched approvals deny. No blanket grants.
- **Hard default-deny**: payment, credential, account and permission actions
  are never executable regardless of approvals.
- **Hard caps**: max actions per run, max run duration.
- **Kill switch**: `kill()` is idempotent, immediate, and sticky — only an
  explicit `reset()` re-arms the service.
- **Full audit**: every action — executed, denied, refused, or killed — is
  recorded and queryable.

The action executor is injected at composition (`executor` callable); this
module never imports pyautogui/pynput directly, keeping the risky surface
behind one seam.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from agent_system.services.permissions import (
    ApprovalRequest,
    Decision,
    PermissionGate,
    Risk,
)

MAX_ACTIONS_PER_RUN = 50
MAX_RUN_SECONDS = 300.0

ALLOWED_ACTIONS = ("click", "type_text", "key_press", "scroll", "move_mouse")

# Never automatable, regardless of approvals (default-deny, v3.1 §27).
FORBIDDEN_ACTIONS = (
    "pay",
    "purchase",
    "checkout",
    "send_email",
    "delete_account",
    "change_password",
    "grant_permission",
)


class AutopilotDisabledError(RuntimeError):
    pass


class AutopilotKilledError(RuntimeError):
    pass


class AutopilotForbiddenError(ValueError):
    pass


@dataclass
class AutopilotAction:
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    target: str = ""  # human-readable target description for the audit log


@dataclass
class AutopilotAuditEntry:
    seq: int
    timestamp: str
    action: str
    target: str
    outcome: str
    detail: str = ""


class AutopilotService:
    """Approval-gated desktop automation. Safe to instantiate anywhere —
    it does nothing unless explicitly enabled AND driven through `run()`."""

    def __init__(
        self,
        gate: PermissionGate,
        executor: Any = None,
        enabled: bool = False,
        max_actions: int = MAX_ACTIONS_PER_RUN,
        max_run_seconds: float = MAX_RUN_SECONDS,
    ) -> None:
        self._gate = gate
        self._executor = executor
        self._enabled = enabled
        self._max_actions = max_actions
        self._max_run_seconds = max_run_seconds
        self._killed = threading.Event()
        self._audit: deque[AutopilotAuditEntry] = deque(maxlen=1000)
        self._seq = 0
        self._lock = threading.Lock()

    # -- state ---------------------------------------------------------------

    @property
    def is_enabled(self) -> bool:
        return self._enabled and not self._killed.is_set()

    def is_killed(self) -> bool:
        """Public kill-switch state (prefer over private _killed access)."""
        return self._killed.is_set()

    def enable(self) -> None:
        if self._killed.is_set():
            raise AutopilotKilledError("kill switch engaged — explicit reset required")
        self._enabled = True

    def kill(self) -> None:
        """Kill switch: idempotent, immediate, sticky until reset()."""
        self._killed.set()
        self._enabled = False
        self._audit_append("kill_switch", "service", "refused_killed", "engaged")

    def reset(self) -> None:
        """Explicit, deliberate re-arm after a kill."""
        self._killed.clear()

    def audit(self) -> list[dict[str, Any]]:
        return [
            {
                "seq": e.seq,
                "timestamp": e.timestamp,
                "action": e.action,
                "target": e.target,
                "outcome": e.outcome,
                "detail": e.detail,
            }
            for e in self._audit
        ]

    # -- approval flow ---------------------------------------------------------

    def request_action_approval(
        self,
        action: AutopilotAction,
        session_id: str | None = None,
        task_id: str | None = None,
        requester: str = "AutopilotAgent",
    ) -> Any:
        """Create the gate record an operator must approve before run()."""
        return self._gate.request(
            ApprovalRequest(
                requested_action=f"autopilot:{action.action}",
                risk=Risk.CRITICAL,
                scope=f"desktop:{action.action}",
                requester=requester,
                task_id=task_id,
                session_id=session_id,
                context={"target": action.target, "params_keys": sorted(action.params.keys())},
            )
        )

    # -- execution -----------------------------------------------------------

    def run(
        self,
        actions: list[tuple[AutopilotAction, str]],
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Execute actions, each bound to a fresh APPROVED approval id.

        `actions` is a list of (action, approval_id) pairs. Any approval that
        is missing, pending, expired, denied, or bound to a different action
        denies that step. Caps and the kill switch abort the remainder.
        """
        from agent_system.domain.events import utcnow

        started = utcnow()
        executed = 0
        results: list[dict[str, Any]] = []

        if not self.is_enabled:
            reason = "kill switch engaged" if self._killed.is_set() else "disabled by default"
            killed = self._killed.is_set()
            for a, _aid in actions:
                self._audit_append(
                    a.action,
                    a.target,
                    "refused_killed" if killed else "refused_disabled",
                    reason,
                )
            if self._killed.is_set():
                raise AutopilotKilledError(f"autopilot {reason}")
            raise AutopilotDisabledError(f"autopilot {reason}")

        for index, (action, approval_id) in enumerate(actions):
            if self._killed.is_set():
                results.append(
                    {"index": index, "action": action.action, "outcome": "refused_killed"}
                )
                break
            if executed >= self._max_actions:
                results.append({"index": index, "action": action.action, "outcome": "cap_reached"})
                break
            elapsed = (utcnow() - started).total_seconds()
            if elapsed > self._max_run_seconds:
                results.append({"index": index, "action": action.action, "outcome": "timeout"})
                break

            # Hard default-deny list — cannot be approved away.
            lowered = action.action.lower()
            forbidden = any(bad in lowered for bad in FORBIDDEN_ACTIONS)
            if forbidden or action.action not in ALLOWED_ACTIONS:
                self._audit_append(
                    action.action, action.target, "forbidden", "default-deny/unknown action"
                )
                results.append({"index": index, "action": action.action, "outcome": "forbidden"})
                continue

            # Approval binding check: fresh APPROVED record for THIS action.
            record = self._gate.get(approval_id)
            bound_action = f"autopilot:{action.action}"
            valid = (
                record is not None
                and record.decision == Decision.APPROVED
                and not record.is_expired()
                and record.requested_action == bound_action
            )
            if not valid:
                outcome = "denied_gate"
                self._audit_append(
                    action.action,
                    action.target,
                    outcome,
                    f"approval {approval_id} not fresh/approved/bound",
                )
                results.append(
                    {
                        "index": index,
                        "action": action.action,
                        "outcome": outcome,
                        "approval_id": approval_id,
                    }
                )
                continue

            if self._executor is None:
                self._audit_append(action.action, action.target, "failed", "no executor wired")
                results.append({"index": index, "action": action.action, "outcome": "no_executor"})
                continue

            try:
                self._executor(action.action, action.params)
                executed += 1
                self._audit_append(action.action, action.target, "executed", "")
                results.append({"index": index, "action": action.action, "outcome": "executed"})
            except Exception as exc:  # noqa: BLE001 — audit everything
                self._audit_append(action.action, action.target, "failed", str(exc)[:200])
                results.append(
                    {
                        "index": index,
                        "action": action.action,
                        "outcome": "failed",
                        "error": str(exc)[:200],
                    }
                )
        return {
            "requested": len(actions),
            "executed": executed,
            "results": results,
            "killed": self._killed.is_set(),
        }

    # -- internals -----------------------------------------------------------

    def _audit_append(self, action: str, target: str, outcome: str, detail: str) -> None:
        with self._lock:
            self._seq += 1
            self._audit.append(
                AutopilotAuditEntry(
                    seq=self._seq,
                    timestamp=utcnow_iso(),
                    action=action,
                    target=target,
                    outcome=outcome,
                    detail=detail,
                )
            )


def utcnow_iso() -> str:
    from agent_system.domain.events import utcnow

    return utcnow().isoformat()
