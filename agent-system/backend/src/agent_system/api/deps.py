"""Shared FastAPI dependency providers.

Lives outside main.py to avoid an import cycle: main.py imports the v1
routers, which import these providers.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

from agent_system.services.auth import Authenticator, require_auth
from agent_system.services.permissions import PermissionGate


def get_session_factory(request: Request) -> Any:
    return request.app.state.session_factory


def get_gate(request: Request) -> PermissionGate:
    gate: PermissionGate = request.app.state.gate
    return gate


def get_authenticator(request: Request) -> Authenticator:
    authenticator: Authenticator = request.app.state.authenticator
    require_auth(request, authenticator)
    return authenticator


def get_principal(request: Request) -> Any:
    """Resolve the caller to a Bob principal without trusting client input.

    Local mode returns the single operator. Telegram mode only accepts a
    ``?principal=<telegram_user_id>`` whose incoming bearer token already
    passed ``require_auth``; the user id is then resolved server-side through
    ``IdentityService`` (unknown/blocked users raise 403). Frontend callers
    can never grant themselves a different user id, role, or ownership scope.
    """
    from fastapi import HTTPException

    from agent_system.config import get_settings
    from agent_system.services.identity import OPERATOR, IdentityMode, IdentityService

    authenticator: Authenticator = request.app.state.authenticator
    require_auth(request, authenticator)
    settings = getattr(request.app.state, "settings", None) or get_settings()
    mode = str(getattr(settings, "agent_identity_mode", IdentityMode.LOCAL.value))
    if mode != IdentityMode.TELEGRAM.value:
        return OPERATOR
    raw = request.query_params.get("principal", "")
    telegram_user_id = str(raw or "").strip()
    if not telegram_user_id:
        raise HTTPException(status_code=401, detail="principal required")
    factory = request.app.state.session_factory
    identity = IdentityService(factory, settings)
    principal = identity.resolve(telegram_user_id)
    if principal is None or not principal.is_authenticated:
        raise HTTPException(status_code=403, detail="unknown or blocked principal")
    return principal


def enforce_session_visible(db: Any, session_id: str, principal: Any) -> Any:
    """Return a session iff the principal owns it (or is the local operator).

    Local mode keeps the single-operator behavior, including legacy rows with
    ``owner_user_id`` NULL. Telegram mode returns 404 for rows the caller
    does not own (never 403 — resource existence must not leak across users).
    """
    from fastapi import HTTPException

    from agent_system.infra.models import Session
    from agent_system.services.identity import IdentityMode

    row = db.get(Session, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    if getattr(principal, "mode", IdentityMode.LOCAL) is IdentityMode.LOCAL:
        return row
    if principal.user_id is None or row.owner_user_id != principal.user_id:
        raise HTTPException(status_code=404, detail="session not found")
    return row


def enforce_task_visible(db: Any, task_id: str, principal: Any) -> Any:
    """Return a task (plus its session) iff the principal owns both rows."""
    from fastapi import HTTPException

    from agent_system.infra.models import Task
    from agent_system.services.identity import IdentityMode

    row = db.get(Task, task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    if getattr(principal, "mode", IdentityMode.LOCAL) is IdentityMode.LOCAL:
        return row
    enforce_session_visible(db, row.session_id, principal)
    if principal.user_id is None or row.owner_user_id != principal.user_id:
        raise HTTPException(status_code=404, detail="task not found")
    return row
