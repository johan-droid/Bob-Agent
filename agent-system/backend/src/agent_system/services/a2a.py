"""Agent-to-agent (A2A) handoff — minimal outbound delegation protocol.

Off by default (``A2A_ENABLED=false``). One working round-trip:

1. Caller invokes ``delegate()`` with a task payload + the external agent's
   URL. The call requires a **fresh, unexpired, APPROVED** gate record bound
   to exactly ``a2a:delegate:<host>``; otherwise a new approval is requested
   and ``A2ANeedsApprovalError`` (carrying the ``approval_id``) is raised —
   the same binding pattern as ``AutopilotService.run``.
2. ``a2a:delegate`` itself is a **default-deny** scope (listed in
   ``DANGEROUS_SCOPES`` alongside the §8.8 scopes): it can never be
   blanket-approved via ``ALLOW_ALWAYS`` — every target host needs its own
   approval, exactly like ``autopilot:input`` vs ``desktop:<action>``.
3. The signed envelope (HMAC-SHA256 over canonical JSON, keyed by
   ``API_SESSION_SECRET``) is POSTed to the external agent with a
   ``callback_url`` for the result. Non-2xx / transport errors raise
   ``A2AError`` — never a fabricated result.
4. The external agent POSTs the signed result envelope to the callback URL
   (``POST /api/v1/a2a/callback``; the HMAC signature IS the auth, same as
   the Telegram webhook header check). ``handle_callback`` verifies the
   signature + freshness (10-minute window), marks the delegation done, flips
   the task row to SUCCEEDED with ``result_json``, and emits ``a2a.result`` +
   ``task.completed``.

Events (all via the bus when factory + bus are provided): ``a2a.delegated``,
``a2a.result``, ``a2a.failed``.
"""

from __future__ import annotations

import hashlib
import hmac
import json as _json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

CALLBACK_FRESHNESS_SECONDS = 600


class A2AError(RuntimeError):
    """Delegation or callback handling failed (surfaced, never silent)."""


class A2ADisabledError(A2AError):
    """Raised when A2A is off (the default)."""


class A2ANeedsApprovalError(A2AError):
    """Raised when delegation needs an approval first.

    Carries the persisted gate ``approval_id`` so the caller surfaces it and
    the user can approve + retry.
    """

    def __init__(self, approval_id: str, action: str) -> None:
        super().__init__(f"awaiting approval {approval_id} for: {action}")
        self.approval_id = approval_id
        self.action = action


def _canonical(body: dict[str, Any]) -> bytes:
    return _json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_envelope(body: dict[str, Any], secret: str) -> dict[str, Any]:
    """Wrap ``body`` with an HMAC-SHA256 signature."""
    if not secret:
        raise A2AError("cannot sign A2A envelope without API_SESSION_SECRET")
    sig = hmac.new(secret.encode("utf-8"), _canonical(body), hashlib.sha256).hexdigest()
    return {"body": body, "signature": sig}


def verify_envelope(
    envelope: dict[str, Any], secret: str, max_age_seconds: int = CALLBACK_FRESHNESS_SECONDS
) -> dict[str, Any]:
    """Verify signature + freshness; return the body or raise ``A2AError``."""
    from agent_system.domain.events import utcnow

    if not isinstance(envelope, dict) or "body" not in envelope or "signature" not in envelope:
        raise A2AError("malformed A2A envelope (need body + signature)")
    body = envelope["body"]
    if not isinstance(body, dict):
        raise A2AError("malformed A2A envelope body")
    if not secret:
        raise A2AError("cannot verify A2A envelope without API_SESSION_SECRET")
    expected = hmac.new(secret.encode("utf-8"), _canonical(body), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(str(envelope["signature"]), expected):
        raise A2AError("A2A signature mismatch (forged or wrong secret)")
    issued = body.get("issued_at")
    if issued is None:
        raise A2AError("A2A envelope missing issued_at (replay protection required)")
    try:
        from datetime import datetime

        issued_at = datetime.fromisoformat(str(issued))
        age = (utcnow() - issued_at).total_seconds()
        if abs(age) > max_age_seconds:
            raise A2AError("stale A2A envelope (replay window exceeded)")
    except A2AError:
        raise
    except Exception as exc:
        raise A2AError(f"unparseable A2A issued_at: {exc}") from exc
    return body


@dataclass
class Delegation:
    delegation_id: str
    task_id: str
    session_id: str | None
    agent_url: str
    status: str = "in_flight"  # in_flight | done | failed
    approval_id: str | None = None
    result: dict[str, Any] | None = None


class A2AService:
    """Minimal outbound delegation, gated by PermissionGate (default off)."""

    def __init__(
        self,
        settings: Any,
        gate: Any,
        factory: Any | None = None,
        event_bus: Any | None = None,
    ) -> None:
        self._settings = settings
        self._gate = gate
        self._factory = factory
        self._bus = event_bus
        self._delegations: dict[str, Delegation] = {}

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._settings, "a2a_enabled", False))

    @property
    def _secret(self) -> str:
        return str(getattr(self._settings, "api_session_secret", "") or "")

    def _scope_for(self, agent_url: str) -> str:
        host = urlparse(agent_url).netloc or agent_url
        return f"a2a:delegate:{host}"

    def delegate(
        self,
        task_id: str,
        agent_url: str,
        payload: dict[str, Any],
        callback_url: str,
        session_id: str | None = None,
        approval_id: str | None = None,
        requester: str = "agent",
    ) -> Delegation:
        """Delegate a task to an external agent (approval-gated, signed)."""
        from agent_system.domain import ids
        from agent_system.domain.events import utcnow
        from agent_system.services.permissions import (
            ApprovalRequest,
            Decision,
            Risk,
        )

        if not self.enabled:
            raise A2ADisabledError("A2A delegation is off (A2A_ENABLED=false)")
        if not agent_url.startswith(("http://", "https://")):
            raise A2AError(f"refusing non-HTTP agent_url: {agent_url}")
        action = f"a2a:delegate:{urlparse(agent_url).netloc or agent_url}:{task_id}"
        scope = self._scope_for(agent_url)
        record = self._gate.get(approval_id) if approval_id else None
        valid = (
            record is not None
            and record.decision == Decision.APPROVED
            and not record.is_expired()
            and record.requested_action == action
        )
        if not valid:
            requested = self._gate.request(
                ApprovalRequest(
                    requested_action=action,
                    risk=Risk.HIGH,
                    scope=scope,
                    requester=requester,
                    task_id=task_id,
                    session_id=session_id,
                )
            )
            raise A2ANeedsApprovalError(requested.approval_id, action)
        delegation_id = ids.new_id("a2a")
        body = {
            "delegation_id": delegation_id,
            "task_id": task_id,
            "session_id": session_id,
            "payload": payload,
            "callback_url": callback_url,
            "issued_at": utcnow().isoformat(),
        }
        envelope = sign_envelope(body, self._secret)
        import httpx

        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(agent_url, json=envelope)
                resp.raise_for_status()
        except Exception as exc:
            self._emit(
                "a2a.failed",
                {"delegation_id": delegation_id, "task_id": task_id, "error": str(exc)[:300]},
                session_id,
                task_id,
            )
            raise A2AError(f"delegation {delegation_id} delivery failed: {exc}") from exc
        delegation = Delegation(
            delegation_id=delegation_id,
            task_id=task_id,
            session_id=session_id,
            agent_url=agent_url,
            approval_id=approval_id,
        )
        self._delegations[delegation_id] = delegation
        self._emit(
            "a2a.delegated",
            {"delegation_id": delegation_id, "task_id": task_id, "agent_url": agent_url},
            session_id,
            task_id,
        )
        return delegation

    def handle_callback(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """Verify a result envelope and complete the task (callback URL target)."""
        import hashlib as _hashlib
        import json as _json

        from agent_system.domain.events import utcnow

        body = verify_envelope(envelope, self._secret)
        # Replay dedup: same signed body delivered N times processes once.
        envelope_hash = _hashlib.sha256(
            _json.dumps(body, sort_keys=True, default=str).encode()
        ).hexdigest()
        if self._factory is not None:
            try:
                from agent_system.infra.db import session_scope
                from agent_system.infra.models import A2AProcessedEnvelope

                with session_scope(self._factory) as db:
                    if db.get(A2AProcessedEnvelope, envelope_hash) is not None:
                        delegation_id = str(body.get("delegation_id") or "")
                        return {
                            "delegation_id": delegation_id,
                            "task_id": self._delegations.get(delegation_id).task_id
                            if self._delegations.get(delegation_id)
                            else None,
                            "status": "duplicate",
                        }
                    db.add(
                        A2AProcessedEnvelope(
                            envelope_hash=envelope_hash,
                            delegation_id=str(body.get("delegation_id") or "")[:80],
                            created_at=utcnow(),
                        )
                    )
            except Exception:
                pass
        delegation_id = str(body.get("delegation_id") or "")
        delegation = self._delegations.get(delegation_id)
        if delegation is None:
            raise A2AError(f"unknown delegation '{delegation_id}'")
        raw_result = body.get("result")
        result = raw_result if isinstance(raw_result, dict) else {"_value": raw_result}
        delegation.status = "done"
        delegation.result = result
        task_id = delegation.task_id
        transitioned = False
        if self._factory is not None:
            from agent_system.domain.tasks import TaskState, validate_transition
            from agent_system.infra.db import session_scope
            from agent_system.infra.models import Task

            with session_scope(self._factory) as db:
                task = db.get(Task, task_id)
                if task is not None and TaskState(task.state) == TaskState.RUNNING:
                    validate_transition(TaskState.RUNNING, TaskState.SUCCEEDED)
                    task.state = TaskState.SUCCEEDED.value
                    task.completed_at = utcnow()
                    task.result_json = result
                    transitioned = True
        # Only emit completion when the task actually transitioned; otherwise
        # a replayed/duplicate callback would emit phantom task.completed.
        if not transitioned:
            return {"delegation_id": delegation_id, "task_id": task_id, "status": "done"}
        self._emit(
            "a2a.result",
            {"delegation_id": delegation_id, "task_id": task_id},
            delegation.session_id,
            task_id,
        )
        self._emit(
            "task.completed",
            {"task_id": task_id, "via": "a2a", "delegation_id": delegation_id},
            delegation.session_id,
            task_id,
        )
        return {"delegation_id": delegation_id, "task_id": task_id, "status": "done"}

    def get(self, delegation_id: str) -> Delegation | None:
        return self._delegations.get(delegation_id)

    def list(self) -> list[Delegation]:
        return list(self._delegations.values())

    def _emit(
        self,
        event_type: str,
        payload: dict[str, Any],
        session_id: str | None,
        task_id: str | None,
    ) -> None:
        if self._factory is None or self._bus is None:
            return
        try:
            from agent_system.domain.events import Event
            from agent_system.infra.db import session_scope

            with session_scope(self._factory) as db:
                self._bus.emit(
                    Event(
                        type=event_type,
                        session_id=session_id,
                        task_id=task_id,
                        actor="a2a",
                        payload=payload,
                    ),
                    db,
                )
        except Exception:
            pass  # event emission must never break delegation


__all__ = [
    "A2AError",
    "A2ADisabledError",
    "A2ANeedsApprovalError",
    "A2AService",
    "Delegation",
    "sign_envelope",
    "verify_envelope",
]
