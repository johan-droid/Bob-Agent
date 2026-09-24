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
    """Resolve the caller to a Bob principal with token binding.

    Local mode returns the single operator. Telegram mode:
    - User-bound bearer (minted via POST /auth/token/user) wins: the token's
      embedded Bob user_id is loaded directly; a ``?principal=`` that disagrees
      is rejected with 403 (prevents horizontal impersonation with a stolen
      shared bearer).
    - Legacy shared bearer + ``?principal=<telegram_user_id>`` still works
      (backward compat) but is logged as deprecated; operators should migrate
      to user-bound tokens.
    """
    import logging as _logging

    from fastapi import HTTPException

    from agent_system.config import get_settings
    from agent_system.services.auth import _raw_token
    from agent_system.services.identity import OPERATOR, IdentityMode, IdentityService

    authenticator = request.app.state.authenticator
    from agent_system.services.auth import require_auth as _require_auth

    _require_auth(request, authenticator)
    settings = getattr(request.app.state, "settings", None) or get_settings()
    mode = str(getattr(settings, "agent_identity_mode", IdentityMode.LOCAL.value))
    if mode != IdentityMode.TELEGRAM.value:
        return OPERATOR
    factory = request.app.state.session_factory
    identity = IdentityService(factory, settings)
    raw_token = _raw_token(request)
    bound_owner: Any = None
    try:
        bound_owner = authenticator.owner_of(raw_token or "")
    except Exception:
        bound_owner = None
    raw = request.query_params.get("principal", "")
    telegram_user_id = str(raw or "").strip()
    if bound_owner is not None and bound_owner is not False:
        # User-bound token: load principal by Bob user_id directly.
        from agent_system.infra.db import session_scope
        from agent_system.infra.models import User

        with session_scope(factory) as db:
            user = db.get(User, str(bound_owner))
            if user is None or not user.is_active:
                raise HTTPException(status_code=403, detail="bound user revoked")
            # If caller also sent ?principal=, it must resolve to the same user.
            if telegram_user_id:
                other = identity.resolve(telegram_user_id)
                if other is None or other.user_id != user.id:
                    raise HTTPException(status_code=403, detail="principal mismatch")
            # Return canonical principal for the bound user (resolve via
            # telegram account when available, else minimal principal).
            from agent_system.infra.models import TelegramAccount

            acct = db.query(TelegramAccount).filter_by(user_id=user.id).first()
            if acct is not None:
                principal = identity.resolve(str(acct.telegram_user_id))
                if principal is not None:
                    return principal
            # Fallback: construct lightweight principal-like object.
            from agent_system.services.identity import Principal, Role

            return Principal(
                user_id=user.id,
                role=Role(user.role),
                mode=IdentityMode.TELEGRAM,
                chat_id=None,
                telegram_user_id=(str(acct.telegram_user_id) if acct else None),
            )
    # Legacy shared bearer path (deprecated, impersonation-capable).
    if not telegram_user_id:
        raise HTTPException(status_code=401, detail="principal required")
    _logging.getLogger(__name__).warning(
        "legacy shared-bearer principal=%s (migrate to user-bound token)", telegram_user_id
    )
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


def _is_telegram(principal: Any) -> bool:
    try:
        from agent_system.services.identity import IdentityMode

        return getattr(principal, "mode", IdentityMode.LOCAL) is not IdentityMode.LOCAL
    except Exception:
        return False


def owner_id(principal: Any) -> str | None:
    return getattr(principal, "user_id", None)


def apply_owner_filter(query: Any, model: Any, principal: Any) -> Any:
    """Filter a query to the principal's rows in telegram mode (no-op local)."""
    if not _is_telegram(principal):
        return query
    uid = owner_id(principal)
    if uid is None:
        # No user -> see nothing (fail-closed).
        return query.filter(model.owner_user_id == "__none__")
    return query.filter(model.owner_user_id == uid)


def enforce_owner_row(row: Any, principal: Any, kind: str = "object") -> Any:
    """404 unless the principal owns the row (local mode bypasses)."""
    from fastapi import HTTPException

    if row is None:
        raise HTTPException(status_code=404, detail=f"{kind} not found")
    if not _is_telegram(principal):
        return row
    uid = owner_id(principal)
    row_owner = getattr(row, "owner_user_id", None)
    # Transitive ownership: tasks/sessions/events linked via session/task.
    if row_owner is None:
        for attr in ("session_id", "task_id"):
            _ = getattr(row, attr, None)
        # If the model has no owner column value, deny in telegram mode
        # unless a transitive check already passed upstream. Fail-closed.
        raise HTTPException(status_code=404, detail=f"{kind} not found")
    if uid is None or row_owner != uid:
        raise HTTPException(status_code=404, detail=f"{kind} not found")
    return row


def enforce_transitive_task(db: Any, task_id: str | None, principal: Any) -> None:
    """404 unless the principal owns the task (and its session)."""
    if task_id is None or not _is_telegram(principal):
        return
    enforce_task_visible(db, str(task_id), principal)


def enforce_transitive_session(db: Any, session_id: str | None, principal: Any) -> None:
    if session_id is None or not _is_telegram(principal):
        return
    enforce_session_visible(db, str(session_id), principal)
