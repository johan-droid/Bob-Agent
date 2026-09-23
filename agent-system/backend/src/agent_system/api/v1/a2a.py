"""Agent-to-agent handoff API.

- POST /api/v1/a2a/delegate (auth): delegate a task to an external agent
  (approval-gated, signed envelope; 503 when A2A_ENABLED=false).
- POST /api/v1/a2a/callback (no auth — the HMAC signature IS the auth, same
  pattern as the Telegram webhook): receive the external agent's result.
- GET  /api/v1/a2a/delegations (auth): list delegations known to this process.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from agent_system.api.deps import get_authenticator
from agent_system.services.a2a import A2AError

a2a_router = APIRouter(prefix="/api/v1/a2a")


class DelegateBody(BaseModel):
    task_id: str
    agent_url: str
    payload: dict[str, Any] = {}
    callback_url: str
    session_id: str | None = None
    approval_id: str | None = None


def _service(request: Request) -> Any:
    svc = getattr(request.app.state, "a2a", None)
    if svc is None or not svc.enabled:
        raise HTTPException(status_code=503, detail="A2A delegation is off (A2A_ENABLED=false)")
    return svc


@a2a_router.post("/delegate", dependencies=[Depends(get_authenticator)])
async def a2a_delegate(body: DelegateBody, request: Request) -> dict[str, Any]:
    """Delegate a task (needs a fresh per-target approval — see A2ANeedsApprovalError)."""
    from agent_system.services.a2a import A2ADisabledError, A2ANeedsApprovalError

    svc = _service(request)
    try:
        delegation = svc.delegate(
            task_id=body.task_id,
            agent_url=body.agent_url,
            payload=body.payload,
            callback_url=body.callback_url,
            session_id=body.session_id,
            approval_id=body.approval_id,
        )
    except A2ANeedsApprovalError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": str(exc), "approval_id": exc.approval_id},
        ) from exc
    except (A2ADisabledError, A2AError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "delegation_id": delegation.delegation_id,
        "task_id": delegation.task_id,
        "status": delegation.status,
    }


@a2a_router.post("/callback")
async def a2a_callback(request: Request) -> dict[str, Any]:
    """Receive an external agent's signed result (HMAC-verified, no bearer needed)."""
    import logging as _logging

    svc = getattr(request.app.state, "a2a", None)
    if svc is None or not svc.enabled:
        raise HTTPException(status_code=503, detail="A2A delegation is off (A2A_ENABLED=false)")
    # Body-size cap (signed envelopes are small).
    try:
        clen = request.headers.get("content-length")
        if clen is not None and int(clen) > 256 * 1024:
            raise HTTPException(status_code=413, detail="envelope too large")
    except ValueError:
        pass
    try:
        envelope: dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body") from None
    try:
        outcome: dict[str, Any] = svc.handle_callback(envelope)
        return outcome
    except A2AError as exc:
        raise HTTPException(status_code=400, detail="invalid envelope") from exc
    except Exception:
        _logging.getLogger(__name__).exception("a2a callback failed")
        raise HTTPException(status_code=500, detail="internal error") from None


@a2a_router.get("/delegations", dependencies=[Depends(get_authenticator)])
async def a2a_list(request: Request) -> dict[str, Any]:
    """List delegations known to this process."""
    svc = _service(request)
    return {
        "delegations": [
            {
                "delegation_id": d.delegation_id,
                "task_id": d.task_id,
                "agent_url": d.agent_url,
                "status": d.status,
            }
            for d in svc.list()
        ]
    }
