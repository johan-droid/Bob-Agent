"""API v1 routers (v3.1 §16).

Every endpoint: versioned path, typed request/response (Pydantic), documented
errors, auth. Sessions/tasks/approvals/events run on real services + SQLite.
"""

from __future__ import annotations

import logging
import threading as _threading
import time as _time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from agent_system.api.deps import (
    enforce_session_visible,
    enforce_task_visible,
    get_authenticator,
    get_principal,
)
from agent_system.config import get_settings
from agent_system.domain import ids
from agent_system.domain.events import Event, EventSensitivity, utcnow
from agent_system.domain.tasks import InvalidTransitionError, TaskState, validate_transition
from agent_system.infra.db import session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Artifact, Session, Task, Workspace
from agent_system.services.auth import Authenticator
from agent_system.services.orchestrator import Supervisor
from agent_system.services.permissions import (
    ApprovalRequest as GateRequest,
)
from agent_system.services.permissions import (
    Decision,
    PermissionGate,
    Policy,
    Risk,
)
from agent_system.services.planner import PlanningError
from agent_system.services.tools.registry import build_registry as build_tool_registry

router = APIRouter(prefix="/api/v1", dependencies=[])
authenticated = APIRouter(prefix="/api/v1", dependencies=[Depends(get_authenticator)])


# ---------------------------------------------------------------------------
# Health (unauthenticated — used by service managers)
# ---------------------------------------------------------------------------


@router.get("/health")
def health() -> dict[str, str]:
    from agent_system.api.main import api_health

    return api_health()


@router.get("/ready")
def ready(response: Response) -> dict[str, Any]:
    from agent_system.api.main import api_ready

    data = api_ready()
    ok = not (data.get("status") == "not_ready" or not data.get("ready", True))
    if not ok:
        response.status_code = 503
    # Minimal unauth surface: status + per-service ok booleans only.
    # Detailed configured/reachable/last_error stay behind authenticated
    # /api/v1/ready-dependency-check.
    checks = data.get("checks")
    if not isinstance(checks, dict):
        checks = {}
    return {
        "status": data.get("status", "ok"),
        "ready": bool(data.get("ready", ok)),
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# Auth bootstrap
# ---------------------------------------------------------------------------


class TokenRequest(BaseModel):
    session_secret: str


# Simple in-memory rate limiter for the token bootstrap endpoint:
# max 10 attempts per minute per client IP (returns 429 when exceeded).
_TOKEN_ATTEMPTS: dict[str, list[float]] = {}
_TOKEN_LOCK = _threading.Lock()
_TOKEN_MAX_ATTEMPTS = 10
_TOKEN_WINDOW_SECONDS = 60.0

# Generic hot-POST limiter: max 60 creates/min/IP for sessions/tasks.
_HOT_ATTEMPTS: dict[str, list[float]] = {}
_HOT_LOCK = _threading.Lock()
_HOT_MAX = 60
_HOT_WINDOW = 60.0


def _check_hot_rate_limit(request: Request, scope: str) -> None:
    client_ip = request.client.host if request.client else "unknown"
    key = f"{scope}:{client_ip}"
    now = _time.monotonic()
    with _HOT_LOCK:
        hist = [t for t in _HOT_ATTEMPTS.get(key, []) if now - t < _HOT_WINDOW]
        if len(hist) >= _HOT_MAX:
            _HOT_ATTEMPTS[key] = hist
            raise HTTPException(status_code=429, detail="too many requests")
        hist.append(now)
        _HOT_ATTEMPTS[key] = hist


def _check_token_rate_limit(request: Request) -> None:
    client_ip = request.client.host if request.client else "unknown"
    now = _time.monotonic()
    with _TOKEN_LOCK:
        for ip, history in list(_TOKEN_ATTEMPTS.items()):
            if not history or now - history[-1] >= _TOKEN_WINDOW_SECONDS:
                del _TOKEN_ATTEMPTS[ip]
        attempts = [
            t for t in _TOKEN_ATTEMPTS.get(client_ip, []) if now - t < _TOKEN_WINDOW_SECONDS
        ]
        if len(attempts) >= _TOKEN_MAX_ATTEMPTS:
            raise HTTPException(status_code=429, detail="too many token attempts")
        attempts.append(now)
        _TOKEN_ATTEMPTS[client_ip] = attempts


@router.post("/auth/token")
def mint_token(body: TokenRequest, request: Request) -> dict[str, str]:
    _check_token_rate_limit(request)
    auth: Authenticator = request.app.state.authenticator
    expected = get_settings().agent_bootstrap_secret
    if not hmac_compare(body.session_secret, expected):
        raise HTTPException(status_code=403, detail="invalid session secret")
    token = auth.mint_token()
    return {"token": token}


class UserTokenRequest(BaseModel):
    session_secret: str = Field(min_length=1, max_length=500)
    telegram_user_id: str = Field(min_length=1, max_length=40)


@router.post("/auth/token/user")
def mint_user_token(body: UserTokenRequest, request: Request) -> dict[str, str]:
    """Mint a token bound to one Bob user (prevents ?principal impersonation).

    Requires the bootstrap secret AND a provisioned, non-blocked telegram user.
    The returned bearer is bound to that user's Bob user_id; get_principal()
    loads the owner directly and rejects a mismatched ?principal=.
    """
    _check_token_rate_limit(request)
    expected = get_settings().agent_bootstrap_secret
    if not hmac_compare(body.session_secret, expected):
        raise HTTPException(status_code=403, detail="invalid session secret")
    from agent_system.services.identity import IdentityService

    settings = get_settings()
    factory = request.app.state.session_factory
    identity = IdentityService(factory, settings)
    principal = identity.resolve(body.telegram_user_id.strip())
    if principal is None or not principal.is_authenticated or principal.user_id is None:
        raise HTTPException(status_code=403, detail="unknown or blocked principal")
    auth: Authenticator = request.app.state.authenticator
    return {"token": auth.mint_user_token(principal.user_id)}


def hmac_compare(a: str, b: str) -> bool:
    import hmac as _hmac

    return _hmac.compare_digest(a.encode(), b.encode())


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class SessionCreate(BaseModel):
    goal: str = Field(min_length=1, max_length=10_000)


class SessionOut(BaseModel):
    id: str
    goal: str
    status: str


@authenticated.post("/sessions", status_code=201)
def create_session(
    body: SessionCreate, request: Request, principal: Annotated[Any, Depends(get_principal)]
) -> SessionOut:
    _check_hot_rate_limit(request, "sessions")
    factory = request.app.state.session_factory
    bus: EventBus = request.app.state.event_bus
    session_id = ids.new_session_id()
    with session_scope(factory) as db:
        db.add(
            Session(
                id=session_id,
                goal=body.goal,
                status="ACTIVE",
                owner_user_id=(principal.user_id if principal is not None else None),
            )
        )
        bus.emit(
            Event(
                type="session.created",
                session_id=session_id,
                actor="user",
                payload={"goal": body.goal[:200]},
            ),
            db,
        )
    return SessionOut(id=session_id, goal=body.goal, status="ACTIVE")


@authenticated.get("/sessions")
def list_sessions(
    request: Request,
    principal: Annotated[Any, Depends(get_principal)],
    limit: int = 50,
    offset: int = 0,
) -> list[SessionOut]:
    factory = request.app.state.session_factory
    with session_scope(factory) as db:
        query = db.query(Session)
        mode = getattr(principal, "mode", None)
        if mode is not None and mode.value != "local":
            uid = getattr(principal, "user_id", None)
            if uid is not None:
                query = query.filter(Session.owner_user_id == uid)
        rows = query.order_by(Session.created_at.desc()).offset(offset).limit(limit).all()
        return [SessionOut(id=r.id, goal=r.goal, status=r.status) for r in rows]


@authenticated.get("/sessions/{session_id}")
def get_session(
    session_id: str, request: Request, principal: Annotated[Any | None, Depends(get_principal)]
) -> SessionOut:
    factory = request.app.state.session_factory
    with session_scope(factory) as db:
        row = enforce_session_visible(db, session_id, principal)
        return SessionOut(id=row.id, goal=row.goal, status=row.status)


class SessionUpdate(BaseModel):
    goal: str | None = Field(default=None, min_length=1, max_length=10_000)
    status: str | None = None


@authenticated.patch("/sessions/{session_id}")
def update_session(
    session_id: str,
    body: SessionUpdate,
    request: Request,
    principal: Annotated[Any | None, Depends(get_principal)],
) -> SessionOut:
    factory = request.app.state.session_factory
    bus: EventBus = request.app.state.event_bus
    with session_scope(factory) as db:
        row = enforce_session_visible(db, session_id, principal)
        if body.goal is not None:
            row.goal = body.goal
        if body.status is not None:
            row.status = body.status
        bus.emit(
            Event(
                type="session.updated",
                session_id=session_id,
                actor="user",
                payload={"goal": row.goal[:200], "status": row.status},
            ),
            db,
        )
        return SessionOut(id=row.id, goal=row.goal, status=row.status)


class PlanOut(BaseModel):
    session_id: str
    intent: str
    risk: str
    strategy: str
    warnings: list[str]
    tasks: list[dict[str, Any]]


@authenticated.post("/sessions/{session_id}/plan", status_code=201)
def plan_session(
    session_id: str, request: Request, principal: Annotated[Any | None, Depends(get_principal)]
) -> PlanOut:
    """Plan a session's goal into a task DAG and persist it as tasks.

    Planning (Planner), validation/persistence (Supervisor) and execution
    (Orchestrator) are separate steps; this endpoint performs the first two and
    leaves the tasks QUEUED. An invalid plan fails closed with 400 and the
    session is marked PLANNING_FAILED rather than executing a bad DAG.
    """
    factory = request.app.state.session_factory
    bus: EventBus = request.app.state.event_bus
    settings = request.app.state.settings
    supervisor = Supervisor(bus)
    with session_scope(factory) as db:
        row = enforce_session_visible(db, session_id, principal)
        if db.query(Task).filter_by(session_id=session_id).count():
            raise HTTPException(
                status_code=409, detail="session already has tasks; planning is one-shot"
            )
        goal = row.goal
    try:
        _, plan, _task_ids = supervisor.create_planned_session(
            factory, goal, settings=settings, session_id=session_id
        )
    except PlanningError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid plan: {exc}") from exc
    warnings = supervisor.validate_plan(
        plan, available_capabilities=set(build_tool_registry(settings).names())
    )
    return PlanOut(
        session_id=session_id,
        intent=plan.intent,
        risk=plan.risk,
        strategy=plan.strategy,
        warnings=warnings,
        tasks=[task.to_json() for task in plan.tasks],
    )


@authenticated.delete("/sessions/{session_id}", status_code=204)
def delete_session(
    session_id: str, request: Request, principal: Annotated[Any | None, Depends(get_principal)]
) -> None:
    factory = request.app.state.session_factory
    bus: EventBus = request.app.state.event_bus
    with session_scope(factory) as db:
        row = enforce_session_visible(db, session_id, principal)
        tasks = db.query(Task).filter_by(session_id=session_id).all()
        for t in tasks:
            db.delete(t)
        db.delete(row)
        bus.emit(
            Event(
                type="session.deleted",
                session_id=session_id,
                actor="user",
                payload={},
            ),
            db,
        )


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


class TaskCreate(BaseModel):
    session_id: str
    task_type: str = Field(pattern=r"^[A-Za-z0-9_]{1,40}$")
    title: str = Field(min_length=1, max_length=500)
    input: dict[str, Any] = Field(default_factory=dict, max_length=50)
    depends_on: list[str] = Field(default_factory=list, max_length=50)
    agent_type: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=80)


class TaskOut(BaseModel):
    id: str
    session_id: str
    task_type: str
    title: str
    state: str
    agent_type: str | None
    depends_on: list[str]
    attempt: int
    last_error: str | None
    output: str | None = None


def _task_out(row: Task) -> TaskOut:
    output: str | None = None
    try:
        result = row.result_json or {}
        if isinstance(result, dict):
            raw = result.get("output")
            if isinstance(raw, str) and raw.strip():
                output = raw[:2000]
    except Exception:
        output = None
    return TaskOut(
        id=row.id,
        session_id=row.session_id,
        task_type=row.task_type,
        title=row.title,
        state=row.state,
        agent_type=row.agent_type,
        depends_on=row.depends_on_json,
        attempt=row.attempt,
        last_error=row.last_error,
        output=output,
    )


def _fresh_task_out(factory: Any, task_id: str) -> TaskOut | None:
    """Re-read a task row for responses kicked off in-process (no detached state)."""
    with session_scope(factory) as db:
        row = db.get(Task, task_id)
        return _task_out(row) if row is not None else None


@authenticated.post("/tasks", status_code=201)
def create_task(
    body: TaskCreate, request: Request, principal: Annotated[Any | None, Depends(get_principal)]
) -> TaskOut:
    _check_hot_rate_limit(request, "tasks")
    factory = request.app.state.session_factory
    bus: EventBus = request.app.state.event_bus
    # Idempotency (v3.1 §10) — owner-scoped to prevent cross-user oracle/squat.
    if body.idempotency_key:
        with session_scope(factory) as db:
            q = db.query(Task).filter_by(idempotency_key=body.idempotency_key)
            mode = getattr(principal, "mode", None)
            if mode is not None and getattr(mode, "value", "local") != "local":
                uid = getattr(principal, "user_id", None)
                if uid is None:
                    raise HTTPException(status_code=401, detail="principal required")
                q = q.filter(Task.owner_user_id == uid)
            existing = q.first()
            if existing is not None:
                return _task_out(existing)
    task_id = ids.new_task_id()
    try:
        with session_scope(factory) as db:
            _ = enforce_session_visible(db, body.session_id, principal)
            task_input = dict(body.input)
            if "goal" not in task_input:
                task_input["goal"] = body.title
            task = Task(
                id=task_id,
                session_id=body.session_id,
                task_type=body.task_type,
                title=body.title,
                input_json=task_input,
                depends_on_json=body.depends_on,
                agent_type=body.agent_type,
                idempotency_key=body.idempotency_key,
                state=TaskState.PENDING.value,
                owner_user_id=(principal.user_id if principal is not None else None),
            )
            db.add(task)
            bus.emit(
                Event(
                    type="task.created", session_id=body.session_id, task_id=task_id, actor="user"
                ),
                db,
            )
    except IntegrityError:
        # Lost a create race: a duplicate delivery inserted our idempotency
        # key first. The UNIQUE constraint kept it to one row — return that
        # row instead of a 500, so a retried request converges.
        if body.idempotency_key is not None:
            with session_scope(factory) as db:
                q2 = db.query(Task).filter_by(idempotency_key=body.idempotency_key)
                mode2 = getattr(principal, "mode", None)
                if mode2 is not None and getattr(mode2, "value", "local") != "local":
                    uid2 = getattr(principal, "user_id", None)
                    if uid2 is not None:
                        q2 = q2.filter(Task.owner_user_id == uid2)
                existing = q2.first()
                if existing is not None:
                    # Verify session visibility before returning converged row.
                    _ = enforce_session_visible(db, existing.session_id, principal)
                    return _task_out(existing)
        raise HTTPException(status_code=409, detail="conflicting concurrent write") from None
    # Pure create: the task stays PENDING until something explicitly queues it
    # (plan, POST /tasks/{id}/run, or a manual transition). Auto-kicking here
    # would race the explicit lifecycle the contract tests walk.
    return _task_out(task)


@authenticated.get("/tasks")
def list_tasks(
    request: Request,
    principal: Annotated[Any | None, Depends(get_principal)],
    session_id: str | None = None,
    state: str | None = None,
    limit: Annotated[int, Query(le=200)] = 50,
) -> list[TaskOut]:
    factory = request.app.state.session_factory
    with session_scope(factory) as db:
        query = db.query(Task)
        # Owner isolation: telegram principals only see their own rows.
        # Local operator keeps single-operator behavior (incl. legacy NULL rows).
        mode = getattr(principal, "mode", None)
        if mode is not None and getattr(mode, "value", "local") != "local":
            uid = getattr(principal, "user_id", None)
            if uid is None:
                return []
            query = query.filter(Task.owner_user_id == uid)
        if session_id:
            # Session filter must still respect ownership: verify visibility first.
            _ = enforce_session_visible(db, session_id, principal)
            query = query.filter_by(session_id=session_id)
        if state:
            query = query.filter_by(state=state.upper())
        rows = query.order_by(Task.created_at.desc()).limit(limit).all()
        return [_task_out(r) for r in rows]


@authenticated.get("/tasks/{task_id}")
def get_task(
    task_id: str, request: Request, principal: Annotated[Any | None, Depends(get_principal)]
) -> TaskOut:
    factory = request.app.state.session_factory
    with session_scope(factory) as db:
        row = enforce_task_visible(db, task_id, principal)
        return _task_out(row)


class TaskTransition(BaseModel):
    target: TaskState
    reason: str | None = None


@authenticated.post("/tasks/{task_id}/transition")
def transition_task(
    task_id: str,
    body: TaskTransition,
    request: Request,
    principal: Annotated[Any | None, Depends(get_principal)],
) -> TaskOut:
    """Explicit, validated state transition — invalid ones are rejected (v3.1 §7).

    Redelivery is idempotent: requesting the state the task is already in
    returns it with no new event and no state change. Concurrent duplicate
    deliveries serialize on a conditional claim — exactly one applies.
    """
    factory = request.app.state.session_factory
    bus: EventBus = request.app.state.event_bus
    with session_scope(factory) as db:
        row = enforce_task_visible(db, task_id, principal)
        current = TaskState(row.state)
        if current == body.target:
            return _task_out(row)
        try:
            validate_transition(current, body.target)
        except InvalidTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if body.target == TaskState.RUNNING:
            # attempt counts "times execution started" — increment on every
            # entry into RUNNING (manual operator transitions included), as a
            # single expression so concurrent claims cannot lose an increment.
            claimed: int = (
                db.query(Task)
                .filter(Task.id == task_id, Task.state == current.value)
                .update(
                    {"state": body.target.value, "attempt": Task.attempt + 1},
                    synchronize_session=False,
                )
            )
        else:
            claimed = (
                db.query(Task)
                .filter(Task.id == task_id, Task.state == current.value)
                .update({"state": body.target.value}, synchronize_session=False)
            )
        if claimed == 0:
            # Lost a concurrent move: reconcile against the winner's state.
            db.refresh(row)
            if TaskState(row.state) == body.target:
                return _task_out(row)
            raise HTTPException(
                status_code=409, detail=f"state changed concurrently to {row.state}"
            )
        db.refresh(row)
        event_type = {
            TaskState.QUEUED: "task.queued",
            TaskState.RUNNING: "task.started",
            TaskState.SUCCEEDED: "task.completed",
            TaskState.FAILED: "task.failed",
            TaskState.CANCELLED: "task.cancelled",
            TaskState.RECOVERING: "task.recovering",
            TaskState.BLOCKED_APPROVAL: "task.blocked_approval",
        }.get(body.target, "task.started")
        bus.emit(
            Event(
                type=event_type,
                session_id=row.session_id,
                task_id=row.id,
                actor="user",
                payload={"from": current.value, "to": body.target.value, "reason": body.reason},
            ),
            db,
        )
    # Pure transition: no implicit execution. Runs are opt-in via
    # POST /tasks/{id}/run (or /retry) so manual lifecycle walks stay exact.
    return _task_out(row)


@authenticated.post("/tasks/{task_id}/retry", status_code=202)
def retry_task(
    task_id: str,
    request: Request,
    response: Response,
    principal: Annotated[Any | None, Depends(get_principal)],
) -> TaskOut:
    """Retry a failed task: FAILED -> QUEUED + execution kickoff.

    Redelivery is idempotent: when the task is already QUEUED (duplicate
    delivery, or a retry after a crashed kickoff) the endpoint re-kicks
    execution and returns 202/200 without a second requeue event. Concurrent
    duplicate retries serialize on a conditional claim — exactly one requeues.
    """
    factory = request.app.state.session_factory
    bus: EventBus = request.app.state.event_bus
    with session_scope(factory) as db:
        row = enforce_task_visible(db, task_id, principal)
        current = TaskState(row.state)
        if current == TaskState.QUEUED:
            response.status_code = 200
        else:
            try:
                validate_transition(current, TaskState.QUEUED)
            except InvalidTransitionError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            claimed: int = (
                db.query(Task)
                .filter(Task.id == task_id, Task.state == current.value)
                .update({"state": TaskState.QUEUED.value}, synchronize_session=False)
            )
            if claimed == 0:
                # Lost a concurrent requeue: reconcile against the winner.
                db.refresh(row)
                if TaskState(row.state) != TaskState.QUEUED:
                    raise HTTPException(
                        status_code=409, detail=f"state changed concurrently to {row.state}"
                    )
                response.status_code = 200
            else:
                db.refresh(row)
                # attempt counts "times execution started" — incremented only at the
                # RUNNING transition (worker/orchestrator), never on requeue.
                bus.emit(
                    Event(
                        type="task.queued",
                        session_id=row.session_id,
                        task_id=row.id,
                        actor="user",
                        payload={"retry": True, "attempt": row.attempt},
                    ),
                    db,
                )
    # Retry is explicit operator intent to run: kick in-process execution.
    # The response is a fresh read; failures land on the task row + events,
    # never on this HTTP call. Settings resolve synchronously here so the
    # worker thread inherits this request's config (single-config execution).
    try:
        from agent_system.config import get_settings
        from agent_system.services.task_runner import kick_task

        kick_task(factory, bus, task_id, settings=get_settings())
    except Exception as exc:
        logging.getLogger(__name__).exception("Retry kickoff failed for %s", task_id)
        raise HTTPException(status_code=503, detail="execution kickoff failed") from exc
    fresh = _fresh_task_out(factory, task_id)
    return fresh if fresh is not None else _task_out(row)


@authenticated.post("/tasks/{task_id}/run", status_code=202)
def run_task(
    task_id: str, request: Request, principal: Annotated[Any | None, Depends(get_principal)]
) -> TaskOut:
    """Explicit execution trigger: queue (if needed) + run in-process.

    This is what the chat UIs call after creating a task. Unlike the bare
    ``transition`` endpoint (pure, no side effects), this one owns the
    PENDING/FAILED -> QUEUED move and then kicks the in-process runner, so a
    local setup with no Redis/RQ worker still executes. Never blocks: the
    run happens on a background thread; progress streams via events.
    """
    factory = request.app.state.session_factory
    bus: EventBus = request.app.state.event_bus
    with session_scope(factory) as db:
        row = enforce_task_visible(db, task_id, principal)
        current = TaskState(row.state)
        if current in (TaskState.PENDING, TaskState.FAILED):
            try:
                validate_transition(current, TaskState.QUEUED)
            except InvalidTransitionError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            # Conditional claim: concurrent cancel/transition wins => 409.
            claimed = (
                db.query(Task)
                .filter(Task.id == task_id, Task.state == current.value)
                .update({"state": TaskState.QUEUED.value}, synchronize_session=False)
            )
            if claimed == 0:
                raise HTTPException(status_code=409, detail="state changed concurrently")
            db.refresh(row)
            bus.emit(
                Event(
                    type="task.queued",
                    session_id=row.session_id,
                    task_id=row.id,
                    actor="user",
                    payload={"run": True, "from": current.value},
                ),
                db,
            )
        elif current != TaskState.QUEUED:
            raise HTTPException(status_code=409, detail=f"cannot run task in state {current.value}")
    try:
        from agent_system.config import get_settings
        from agent_system.services.task_runner import kick_task

        kick_task(factory, bus, task_id, settings=get_settings())
    except Exception as exc:
        logging.getLogger(__name__).exception("Run kickoff failed for %s", task_id)
        raise HTTPException(status_code=503, detail="execution kickoff failed") from exc
    fresh = _fresh_task_out(factory, task_id)
    if fresh is None:
        raise HTTPException(status_code=404, detail="task not found")
    return fresh


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


class ApprovalOut(BaseModel):
    approval_id: str
    requested_action: str
    risk: str
    scope: str
    requester: str
    decision: str
    task_id: str | None
    agent_run_id: str | None
    session_id: str | None
    workspace_id: str | None
    context: dict[str, Any]
    reason: str | None


class ApprovalCreate(BaseModel):
    requested_action: str
    risk: Risk
    scope: str
    requester: str
    task_id: str | None = None
    agent_run_id: str | None = None
    session_id: str | None = None
    workspace_id: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    owner_user_id: str | None = None


class ApprovalDecision(BaseModel):
    approve: bool
    policy: Policy = Policy.ALLOW_ONCE
    reason: str | None = None


def _approval_out(rec: Any) -> ApprovalOut:
    return ApprovalOut(
        approval_id=rec.approval_id,
        requested_action=rec.requested_action,
        risk=rec.risk.value if hasattr(rec.risk, "value") else str(rec.risk),
        scope=rec.scope,
        requester=rec.requester,
        decision=rec.decision.value if hasattr(rec.decision, "value") else str(rec.decision),
        task_id=rec.task_id,
        agent_run_id=rec.agent_run_id,
        session_id=rec.session_id,
        workspace_id=rec.workspace_id,
        context=rec.context,
        reason=rec.reason,
    )


@authenticated.post("/approvals", status_code=202)
def request_approval(
    body: ApprovalCreate, request: Request, principal: Annotated[Any | None, Depends(get_principal)]
) -> ApprovalOut:
    gate: PermissionGate = request.app.state.gate
    bus: EventBus = request.app.state.event_bus
    factory = request.app.state.session_factory
    record = gate.request(
        GateRequest(
            requested_action=body.requested_action,
            risk=body.risk,
            scope=body.scope,
            requester=body.requester,
            task_id=body.task_id,
            agent_run_id=body.agent_run_id,
            session_id=body.session_id,
            workspace_id=body.workspace_id,
            context=body.context,
            owner_user_id=(principal.user_id if principal is not None else None),
        )
    )
    with session_scope(factory) as db:
        bus.emit(
            Event(
                type="approval.requested",
                session_id=body.session_id,
                task_id=body.task_id,
                actor=body.requester,
                payload={
                    "approval_id": record.approval_id,
                    "scope": body.scope,
                    "risk": body.risk.value,
                },
                sensitivity=EventSensitivity.SENSITIVE,
            ),
            db,
        )
    return _approval_out(record)


@authenticated.get("/approvals")
def list_approvals(
    request: Request,
    principal: Annotated[Any | None, Depends(get_principal)],
    pending_only: bool = True,
) -> list[ApprovalOut]:
    gate: PermissionGate = request.app.state.gate
    records = gate.list_pending() if pending_only else gate.list_all()
    mode = getattr(principal, "mode", None)
    if mode is not None and mode.value != "local":
        uid = getattr(principal, "user_id", None)
        if uid is None:
            return []
        # Strict: legacy NULL-owner rows are NOT shared across users.
        records = [r for r in records if r.owner_user_id == uid]
    return [_approval_out(r) for r in records]


@authenticated.post("/approvals/{approval_id}/decision")
def decide_approval(
    approval_id: str,
    body: ApprovalDecision,
    request: Request,
    principal: Annotated[Any | None, Depends(get_principal)],
) -> ApprovalOut:
    gate: PermissionGate = request.app.state.gate
    bus: EventBus = request.app.state.event_bus
    factory = request.app.state.session_factory
    current = gate.get(approval_id)
    if current is None:
        raise HTTPException(status_code=404, detail=f"approval '{approval_id}' not found")
    # Ownership gate BEFORE any content is revealed: a caller must never learn
    # the contents of an approval owned by someone else — including already
    # decided ones (the first-decision-sticks early return below leaks the
    # full record and must not run for a non-owner).
    uid = principal.user_id if principal is not None else None
    if current.owner_user_id is not None and uid is not None and current.owner_user_id != uid:
        raise HTTPException(
            status_code=403, detail=f"approval '{approval_id}' belongs to a different owner"
        )
    if current.decision is not Decision.PENDING:
        # Redelivery: the first decision sticks — report it without emitting
        # a second decision event for the same logical decision.
        return _approval_out(current)
    try:
        record = gate.decide(
            approval_id,
            approve=body.approve,
            policy=body.policy,
            reason=body.reason,
            decided_by_user_id=(principal.user_id if principal is not None else None),
        )
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    event_type = (
        "approval.approved"
        if record.decision == Decision.APPROVED
        else ("approval.denied" if record.decision == Decision.DENIED else "approval.expired")
    )
    try:
        from agent_system.infra.telemetry import elapsed_seconds, get_metrics

        seconds = elapsed_seconds(record.created_at, record.decided_at)
        if seconds is not None:
            get_metrics().record_approval_latency(record.scope, seconds)
    except Exception:
        pass  # telemetry must never break approvals
    with session_scope(factory) as db:
        bus.emit(
            Event(
                type=event_type,
                session_id=record.session_id,
                task_id=record.task_id,
                actor="user",
                payload={"approval_id": record.approval_id},
            ),
            db,
        )
    return _approval_out(record)


@authenticated.post("/approvals/sweep")
def sweep_approvals(
    request: Request, principal: Annotated[Any | None, Depends(get_principal)]
) -> dict[str, object]:
    gate: PermissionGate = request.app.state.gate
    bus: EventBus = request.app.state.event_bus
    factory = request.app.state.session_factory
    expired = gate.sweep_expired()
    with session_scope(factory) as db:
        for approval_id in expired:
            bus.emit(
                Event(type="approval.expired", actor="gate", payload={"approval_id": approval_id}),
                db,
            )
    return {"expired": expired}


# ---------------------------------------------------------------------------
# Workspaces (Phase 5): CRUD + file tree + fingerprint
# ---------------------------------------------------------------------------


class WorkspaceOut(BaseModel):
    id: str
    name: str
    status: str
    size_bytes: int
    file_count: int


@authenticated.post("/workspaces", status_code=201)
def create_workspace(
    body: dict[str, Any],
    request: Request,
    principal: Annotated[Any | None, Depends(get_principal)],
) -> WorkspaceOut:
    from agent_system.api.deps import owner_id
    from agent_system.services.workspaces import WorkspaceManager

    factory = request.app.state.session_factory
    bus: EventBus = request.app.state.event_bus
    settings = request.app.state.settings
    ws_id = ids.new_workspace_id()
    name = str(body.get("name") or ws_id)[:100]
    manager = WorkspaceManager(settings.workspaces_dir)
    manager.create(ws_id)
    with session_scope(factory) as db:
        db.add(Workspace(id=ws_id, name=name, status="CREATED", owner_user_id=owner_id(principal)))
        bus.emit(
            Event(type="workspace.created", actor="user", payload={"workspace_id": ws_id}),
            db,
        )
    return WorkspaceOut(id=ws_id, name=name, status="CREATED", size_bytes=0, file_count=0)


@authenticated.get("/workspaces")
def list_workspaces(
    request: Request, principal: Annotated[Any | None, Depends(get_principal)]
) -> list[WorkspaceOut]:
    from agent_system.api.deps import apply_owner_filter
    from agent_system.services.workspaces import WorkspaceManager

    factory = request.app.state.session_factory
    settings = request.app.state.settings
    manager = WorkspaceManager(settings.workspaces_dir)
    with session_scope(factory) as db:
        rows = apply_owner_filter(
            db.query(Workspace).order_by(Workspace.created_at.desc()), Workspace, principal
        ).all()
        out: list[WorkspaceOut] = []
        for r in rows:
            try:
                files = manager.tree(r.id)
            except Exception:
                files = []
            out.append(
                WorkspaceOut(
                    id=r.id,
                    name=r.name,
                    status=r.status,
                    size_bytes=r.size_bytes,
                    file_count=len(files),
                )
            )
        return out


@authenticated.get("/workspaces/{workspace_id}/tree")
def workspace_tree(
    workspace_id: str,
    request: Request,
    principal: Annotated[Any | None, Depends(get_principal)],
) -> dict[str, Any]:
    from agent_system.api.deps import enforce_owner_row
    from agent_system.services.workspaces import WorkspaceManager

    factory = request.app.state.session_factory
    settings = request.app.state.settings
    manager = WorkspaceManager(settings.workspaces_dir)
    with session_scope(factory) as db:
        enforce_owner_row(db.get(Workspace, workspace_id), principal, "workspace")
    try:
        files = manager.tree(workspace_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"workspace_id": workspace_id, "files": files}


@authenticated.get("/workspaces/{workspace_id}/file")
def read_workspace_file(
    workspace_id: str,
    request: Request,
    path: str,
    principal: Annotated[Any | None, Depends(get_principal)],
) -> dict[str, Any]:
    import base64

    from agent_system.api.deps import enforce_owner_row
    from agent_system.services.workspaces import WorkspaceManager

    factory = request.app.state.session_factory
    settings = request.app.state.settings
    with session_scope(factory) as db:
        enforce_owner_row(db.get(Workspace, workspace_id), principal, "workspace")
    manager = WorkspaceManager(settings.workspaces_dir)
    try:
        content = manager.read_file(workspace_id, path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="file not found") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "path": path,
        "size": len(content),
        "content_b64": base64.b64encode(content).decode(),
    }


class WorkspaceWrite(BaseModel):
    path: str
    content_b64: str


@authenticated.put("/workspaces/{workspace_id}/file")
def write_workspace_file(
    workspace_id: str,
    body: WorkspaceWrite,
    request: Request,
    principal: Annotated[Any | None, Depends(get_principal)],
) -> dict[str, Any]:
    import base64

    from agent_system.api.deps import enforce_owner_row
    from agent_system.services.workspaces import WorkspaceManager

    factory = request.app.state.session_factory
    bus: EventBus = request.app.state.event_bus
    settings = request.app.state.settings
    with session_scope(factory) as db:
        enforce_owner_row(db.get(Workspace, workspace_id), principal, "workspace")
    manager = WorkspaceManager(settings.workspaces_dir)
    try:
        content = base64.b64decode(body.content_b64)
        manager.write_file(workspace_id, body.path, content)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with session_scope(factory) as db:
        ws = db.get(Workspace, workspace_id)
        if ws is not None:
            ws.last_modified_at = utcnow()
        bus.emit(
            Event(
                type="workspace.modified",
                actor="user",
                payload={"workspace_id": workspace_id, "path": body.path},
            ),
            db,
        )
    return {"path": body.path, "bytes": len(content)}


@authenticated.get("/workspaces/{workspace_id}/fingerprint")
def workspace_fingerprint(
    workspace_id: str,
    request: Request,
    principal: Annotated[Any | None, Depends(get_principal)],
) -> dict[str, str]:
    from agent_system.api.deps import enforce_owner_row
    from agent_system.services.workspaces import WorkspaceManager

    factory = request.app.state.session_factory
    settings = request.app.state.settings
    with session_scope(factory) as db:
        enforce_owner_row(db.get(Workspace, workspace_id), principal, "workspace")
    manager = WorkspaceManager(settings.workspaces_dir)
    try:
        return {"workspace_id": workspace_id, "fingerprint": manager.fingerprint(workspace_id)}
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@authenticated.delete("/workspaces/{workspace_id}", status_code=204)
def delete_workspace(
    workspace_id: str,
    request: Request,
    principal: Annotated[Any | None, Depends(get_principal)],
) -> None:
    from agent_system.api.deps import enforce_owner_row
    from agent_system.services.workspaces import WorkspaceManager

    factory = request.app.state.session_factory
    bus: EventBus = request.app.state.event_bus
    settings = request.app.state.settings
    manager = WorkspaceManager(settings.workspaces_dir)
    with session_scope(factory) as db:
        enforce_owner_row(db.get(Workspace, workspace_id), principal, "workspace")
    manager.delete(workspace_id)
    with session_scope(factory) as db:
        ws = db.get(Workspace, workspace_id)
        if ws is not None:
            db.delete(ws)
        bus.emit(
            Event(type="workspace.destroyed", actor="user", payload={"workspace_id": workspace_id}),
            db,
        )


# ---------------------------------------------------------------------------
# Sandbox execution (Phase 5): run a command in the workspace container
# ---------------------------------------------------------------------------


class SandboxExec(BaseModel):
    command: str
    timeout_seconds: int = Field(default=60, ge=1, le=1800)
    network: bool = False


@authenticated.post("/workspaces/{workspace_id}/exec")
def sandbox_exec(
    workspace_id: str,
    body: SandboxExec,
    request: Request,
    principal: Annotated[Any | None, Depends(get_principal)],
) -> dict[str, Any]:
    from agent_system.services.permissions import ApprovalRequest, Risk
    from agent_system.services.sandbox import DockerSandbox, SandboxUnavailableError
    from agent_system.services.workspaces import WorkspaceManager

    bus: EventBus = request.app.state.event_bus
    factory = request.app.state.session_factory
    settings = request.app.state.settings
    gate: Any = getattr(request.app.state, "gate", None)
    # Approval gate: direct exec must not bypass TOOLS_REQUIRE_APPROVAL.
    if gate is not None and bool(getattr(settings, "tools_require_approval", True)):
        scope = f"shell:{body.command[:60]}"
        req = ApprovalRequest(
            requested_action=f"workspace exec: {body.command[:200]}",
            risk=Risk.HIGH,
            scope=scope,
            requester="api",
            workspace_id=workspace_id,
            context={},
            owner_user_id=(getattr(principal, "user_id", None) if principal else None),
        )
        decision = gate.authorize(req)
        if not decision.allowed:
            raise HTTPException(
                status_code=403,
                detail=f"approval required: {decision.approval_id}",
            )
    # Network is never granted via this endpoint: force isolation.
    if body.network:
        raise HTTPException(status_code=403, detail="network exec not allowed via API")
    with session_scope(factory) as db:
        from agent_system.api.deps import enforce_owner_row

        enforce_owner_row(db.get(Workspace, workspace_id), principal, "workspace")
    manager = WorkspaceManager(settings.workspaces_dir)
    try:
        ws_path = manager.path(workspace_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="workspace directory missing") from exc
    try:
        sandbox = DockerSandbox()
    except SandboxUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    result = sandbox.run(
        str(ws_path), body.command, timeout_seconds=body.timeout_seconds, network=False
    )
    with session_scope(factory) as db:
        bus.emit(
            Event(
                type="tool.completed",
                actor="sandbox",
                payload={
                    "workspace_id": workspace_id,
                    "exit_code": result["exit_code"],
                },
            ),
            db,
        )
    return result


# ---------------------------------------------------------------------------
# Artifacts (Phase 7)
# ---------------------------------------------------------------------------


class ArtifactOut(BaseModel):
    id: str
    task_id: str | None
    kind: str
    path: str
    size_bytes: int


@authenticated.get("/artifacts")
def list_artifacts(
    request: Request,
    principal: Annotated[Any | None, Depends(get_principal)],
    task_id: str | None = None,
) -> list[ArtifactOut]:
    from agent_system.api.deps import apply_owner_filter, enforce_transitive_task

    factory = request.app.state.session_factory
    with session_scope(factory) as db:
        if task_id:
            enforce_transitive_task(db, task_id, principal)
        query = db.query(Artifact)
        if task_id:
            query = query.filter_by(task_id=task_id)
        query = apply_owner_filter(query, Artifact, principal)
        rows = query.order_by(Artifact.created_at.desc()).limit(200).all()
        return [
            ArtifactOut(
                id=r.id, task_id=r.task_id, kind=r.kind, path=r.path, size_bytes=r.size_bytes
            )
            for r in rows
        ]


# ---------------------------------------------------------------------------
# Events (resume-from-sequence, v3.1 §17)
# ---------------------------------------------------------------------------


class EventOut(BaseModel):
    event_id: str
    sequence: int
    timestamp: Any
    type: str
    actor: str
    session_id: str | None
    task_id: str | None
    agent_run_id: str | None
    payload: dict[str, Any]
    visibility: str
    sensitivity: str


@authenticated.get("/events")
def list_events(
    request: Request,
    principal: Annotated[Any | None, Depends(get_principal)],
    after_sequence: int = 0,
    type: str | None = None,
    session_id: str | None = None,
    limit: Annotated[int, Query(le=500)] = 200,
) -> list[EventOut]:
    from agent_system.api.deps import _is_telegram, enforce_session_visible, owner_id

    factory = request.app.state.session_factory
    bus: EventBus = request.app.state.event_bus
    with session_scope(factory) as db:
        if session_id and _is_telegram(principal):
            enforce_session_visible(db, session_id, principal)
        # session_id is pushed into the SQL WHERE clause (indexed) instead
        # of Python post-filtering; limit is capped at 500 rows per page.
        events = bus.replay_after(
            db,
            after_sequence=after_sequence,
            event_type=type,
            limit=limit,
            session_id=session_id,
        )
        if _is_telegram(principal):
            # Post-filter to owned sessions/tasks only.
            from agent_system.infra.models import Session, Task

            uid = owner_id(principal)
            owned_sessions = {
                r[0] for r in db.query(Session.id).filter(Session.owner_user_id == uid).all()
            }
            filtered = []
            for e in events:
                if e.session_id is not None:
                    if str(e.session_id) not in owned_sessions:
                        continue
                elif e.task_id is not None:
                    t = db.get(Task, str(e.task_id))
                    if t is None or str(t.session_id) not in owned_sessions:
                        continue
                else:
                    continue  # session-less global events hidden in telegram mode
                filtered.append(e)
            events = filtered
        return [
            EventOut(
                event_id=e.event_id,
                sequence=e.sequence or 0,
                timestamp=e.timestamp,
                type=e.type,
                actor=e.actor,
                session_id=e.session_id,
                task_id=e.task_id,
                agent_run_id=e.agent_run_id,
                payload=e.payload,
                visibility=e.visibility,
                sensitivity=e.sensitivity,
            )
            for e in events
        ]


# ---------------------------------------------------------------------------
# Vault & Memory Notes (Phase 8)
# ---------------------------------------------------------------------------


class VaultNoteOut(BaseModel):
    name: str
    path: str
    layer: str
    title: str
    source: str
    created: str | None = None
    tags: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    size_bytes: int = 0
    task_id: str | None = None
    session_id: str | None = None


class VaultNoteDetail(VaultNoteOut):
    body: str


class VaultNoteCreate(BaseModel):
    title: str
    layer: str = "USER"
    source: str = "user"
    body: str
    tags: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    task_id: str | None = None
    session_id: str | None = None


@authenticated.get("/vault/notes")
def list_vault_notes(
    request: Request,
    principal: Annotated[Any | None, Depends(get_principal)],
    layer: str | None = None,
    search: str | None = None,
) -> list[VaultNoteOut]:
    from agent_system.api.deps import _is_telegram
    from agent_system.api.deps import owner_id as _oid4
    from agent_system.services.memory import MemoryLayer, ObsidianVaultWriter

    settings = request.app.state.settings
    writer = ObsidianVaultWriter(Path(settings.vault_path))
    telegram_mode = _is_telegram(principal)
    caller_owner = _oid4(principal)
    mem_layer = None
    if layer:
        try:
            mem_layer = MemoryLayer(layer.upper())
        except ValueError:
            pass
    note_paths = writer.list_notes(mem_layer)
    out: list[VaultNoteOut] = []
    search_lower = search.lower() if search else None
    for p in note_paths:
        try:
            data = writer.read_note(p)
            fm = data.get("frontmatter") or {}
            if telegram_mode:
                # Fail-closed: hide notes owned by others and legacy notes
                # with no owner stamp.
                if fm.get("owner_user_id") != caller_owner:
                    continue
            title = str(fm.get("title") or p.stem)
            tags = [str(t) for t in fm.get("tags") or []]
            note_layer = str(fm.get("layer") or p.parent.name.upper())
            if search_lower:
                body_sample = str(data.get("body") or "")[:500].lower()
                if (
                    search_lower not in title.lower()
                    and not any(search_lower in t.lower() for t in tags)
                    and search_lower not in body_sample
                ):
                    continue
            rel_path = str(p.relative_to(writer.root))
            out.append(
                VaultNoteOut(
                    name=p.name,
                    path=rel_path,
                    layer=note_layer,
                    title=title,
                    source=str(fm.get("source") or "unknown"),
                    created=str(fm.get("created") or ""),
                    tags=tags,
                    links=[str(link) for link in fm.get("links") or []],
                    size_bytes=p.stat().st_size if p.exists() else 0,
                    task_id=fm.get("task_id"),
                    session_id=fm.get("session_id"),
                )
            )
        except Exception:
            continue
    return out


@authenticated.get("/vault/note")
def get_vault_note(
    request: Request,
    path: str,
    principal: Annotated[Any | None, Depends(get_principal)],
) -> VaultNoteDetail:
    from agent_system.api.deps import _is_telegram
    from agent_system.api.deps import owner_id as _oid5
    from agent_system.services.memory import ObsidianVaultWriter

    settings = request.app.state.settings
    writer = ObsidianVaultWriter(Path(settings.vault_path))
    target = (writer.root / path).resolve()
    try:
        target.relative_to(writer.root.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid path") from None
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="note not found")
    data = writer.read_note(target)
    fm = data.get("frontmatter") or {}
    if _is_telegram(principal) and fm.get("owner_user_id") != _oid5(principal):
        raise HTTPException(status_code=404, detail="note not found")
    rel_path = str(target.relative_to(writer.root))
    return VaultNoteDetail(
        name=target.name,
        path=rel_path,
        layer=str(fm.get("layer") or target.parent.name.upper()),
        title=str(fm.get("title") or target.stem),
        source=str(fm.get("source") or "unknown"),
        created=str(fm.get("created") or ""),
        tags=[str(tag) for tag in fm.get("tags") or []],
        links=[str(link) for link in fm.get("links") or []],
        size_bytes=target.stat().st_size,
        task_id=fm.get("task_id"),
        session_id=fm.get("session_id"),
        body=str(data.get("body") or ""),
    )


@authenticated.post("/vault/notes", status_code=201)
def create_vault_note(
    body: VaultNoteCreate,
    request: Request,
    principal: Annotated[Any | None, Depends(get_principal)],
) -> VaultNoteDetail:
    from agent_system.api.deps import enforce_transitive_session, enforce_transitive_task, owner_id
    from agent_system.services.memory import MemoryLayer, NoteMeta, ObsidianVaultWriter

    if len(body.body) > 100 * 1024:
        raise HTTPException(status_code=413, detail="note body too large")
    settings = request.app.state.settings
    factory = request.app.state.session_factory
    with session_scope(factory) as db:
        enforce_transitive_task(db, body.task_id, principal)
        enforce_transitive_session(db, body.session_id, principal)
    writer = ObsidianVaultWriter(Path(settings.vault_path))
    try:
        layer_enum = MemoryLayer(body.layer.upper())
    except ValueError:
        layer_enum = MemoryLayer.USER

    meta = NoteMeta(
        title=body.title,
        layer=layer_enum,
        source=body.source,
        task_id=body.task_id,
        session_id=body.session_id,
        owner_user_id=owner_id(principal),
        tags=body.tags,
        links=body.links,
    )
    note_path = writer.write_note(meta, body.body)
    data = writer.read_note(note_path)
    fm = data.get("frontmatter") or {}
    rel_path = str(note_path.relative_to(writer.root))
    return VaultNoteDetail(
        name=note_path.name,
        path=rel_path,
        layer=layer_enum.value,
        title=meta.title,
        source=meta.source,
        created=str(fm.get("created") or ""),
        tags=body.tags,
        links=body.links,
        size_bytes=note_path.stat().st_size,
        task_id=body.task_id,
        session_id=body.session_id,
        body=str(data.get("body") or ""),
    )


@authenticated.delete("/vault/notes", status_code=204)
def delete_vault_note(
    request: Request,
    path: str,
    principal: Annotated[Any | None, Depends(get_principal)],
) -> None:
    from agent_system.api.deps import _is_telegram
    from agent_system.api.deps import owner_id as _oid3
    from agent_system.services.memory import ObsidianVaultWriter

    settings = request.app.state.settings
    writer = ObsidianVaultWriter(Path(settings.vault_path))
    target = (writer.root / path).resolve()
    try:
        target.relative_to(writer.root.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid path") from None
    if _is_telegram(principal) and target.exists() and target.is_file():
        try:
            data = writer.read_note(target)
            fm = data.get("frontmatter") or {}
            note_owner = fm.get("owner_user_id")
            uid = _oid3(principal)
            if note_owner is not None and note_owner != uid:
                raise HTTPException(status_code=404, detail="note not found")
            if note_owner is None and uid is not None:
                raise HTTPException(status_code=404, detail="note not found")
        except HTTPException:
            raise
        except Exception:
            pass
    if target.exists() and target.is_file():
        target.unlink()


# ---------------------------------------------------------------------------
# Templates (Phase 14): Snapshot / Restore workspaces
# ---------------------------------------------------------------------------


class TemplateOut(BaseModel):
    template_id: str
    name: str
    size_bytes: int
    created_at: str
    skipped_secrets: list[str] = Field(default_factory=list)


class TemplateCreate(BaseModel):
    workspace_id: str
    name: str


class TemplateRestore(BaseModel):
    workspace_name: str | None = None


@authenticated.get("/templates")
def list_templates(request: Request) -> list[TemplateOut]:
    import json

    settings = request.app.state.settings
    tpl_dir = Path(settings.templates_dir)
    tpl_dir.mkdir(parents=True, exist_ok=True)
    out: list[TemplateOut] = []
    for archive in sorted(tpl_dir.glob("*.tar.gz")):
        tpl_id = archive.name[:-7]
        meta_file = tpl_dir / f"{tpl_id}.json"
        name = tpl_id
        skipped: list[str] = []
        created_str = datetime.fromtimestamp(archive.stat().st_mtime, tz=UTC).isoformat()
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                name = meta.get("name", tpl_id)
                skipped = meta.get("skipped", [])
                created_str = meta.get("created_at", created_str)
            except Exception:
                pass
        out.append(
            TemplateOut(
                template_id=tpl_id,
                name=name,
                size_bytes=archive.stat().st_size,
                created_at=created_str,
                skipped_secrets=skipped,
            )
        )
    return out


@authenticated.post("/templates", status_code=201)
def create_template(
    body: TemplateCreate,
    request: Request,
    principal: Annotated[Any | None, Depends(get_principal)],
) -> TemplateOut:
    import json

    from agent_system.api.deps import enforce_owner_row, owner_id
    from agent_system.services.workspaces import TemplateManager, WorkspaceManager

    settings = request.app.state.settings
    factory = request.app.state.session_factory
    with session_scope(factory) as db:
        enforce_owner_row(db.get(Workspace, body.workspace_id), principal, "workspace")
    ws_mgr = WorkspaceManager(settings.workspaces_dir)
    ws_path = ws_mgr.path(body.workspace_id)
    if not ws_path.exists():
        raise HTTPException(status_code=404, detail="workspace not found")

    tpl_mgr = TemplateManager(Path(settings.templates_dir))
    tpl_id = ids.new_id("tpl_")
    archive = tpl_mgr.snapshot(tpl_id, ws_path, body.name)
    meta_file = Path(settings.templates_dir) / f"{tpl_id}.json"
    now_str = datetime.now(UTC).isoformat()
    meta_file.write_text(
        json.dumps(
            {
                "name": body.name,
                "workspace_id": body.workspace_id,
                "created_at": now_str,
                "skipped": tpl_mgr.last_skipped,
                "owner_user_id": owner_id(principal),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return TemplateOut(
        template_id=tpl_id,
        name=body.name,
        size_bytes=archive.stat().st_size,
        created_at=now_str,
        skipped_secrets=tpl_mgr.last_skipped,
    )


@authenticated.post("/templates/{template_id}/restore", status_code=201)
def restore_template(
    template_id: str,
    body: TemplateRestore,
    request: Request,
    principal: Annotated[Any | None, Depends(get_principal)],
) -> WorkspaceOut:
    import json as _json

    from agent_system.api.deps import _is_telegram, owner_id
    from agent_system.services.workspaces import TemplateManager, WorkspaceManager

    factory = request.app.state.session_factory
    bus: EventBus = request.app.state.event_bus
    settings = request.app.state.settings
    # Owner check on file-based template meta (fail-closed in telegram mode).
    if _is_telegram(principal):
        meta = Path(settings.templates_dir) / f"{template_id}.json"
        try:
            data = _json.loads(meta.read_text(encoding="utf-8"))
            meta_owner = data.get("owner_user_id")
            uid = owner_id(principal)
            if meta_owner is not None and meta_owner != uid:
                raise HTTPException(status_code=404, detail="template not found")
            if meta_owner is None and uid is not None:
                raise HTTPException(status_code=404, detail="template not found")
        except HTTPException:
            raise
        except Exception:
            pass
    ws_id = ids.new_workspace_id()
    name = str(body.workspace_name or f"restored-{template_id[:8]}")[:100]

    ws_mgr = WorkspaceManager(settings.workspaces_dir)
    target_path = ws_mgr.create(ws_id)

    tpl_mgr = TemplateManager(Path(settings.templates_dir))
    try:
        restored_files = tpl_mgr.clone(template_id, target_path)
    except Exception as exc:
        ws_mgr.delete(ws_id)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    with session_scope(factory) as db:
        from agent_system.api.deps import owner_id as _oid

        db.add(Workspace(id=ws_id, name=name, status="CREATED", owner_user_id=_oid(principal)))
        bus.emit(
            Event(
                type="workspace.restored",
                actor="user",
                payload={
                    "workspace_id": ws_id,
                    "template_id": template_id,
                    "files": len(restored_files),
                },
            ),
            db,
        )
    return WorkspaceOut(
        id=ws_id,
        name=name,
        status="CREATED",
        size_bytes=sum(p.stat().st_size for p in target_path.rglob("*") if p.is_file()),
        file_count=len(restored_files),
    )


@authenticated.delete("/templates/{template_id}", status_code=204)
def delete_template(
    template_id: str,
    request: Request,
    principal: Annotated[Any | None, Depends(get_principal)],
) -> None:
    import json as _json2

    from agent_system.api.deps import _is_telegram
    from agent_system.api.deps import owner_id as _oid2

    settings = request.app.state.settings
    if _is_telegram(principal):
        meta = Path(settings.templates_dir) / f"{template_id}.json"
        try:
            data = _json2.loads(meta.read_text(encoding="utf-8"))
            owner = data.get("owner_user_id")
            me = _oid2(principal)
            if owner is not None and me is not None and owner != me:
                raise HTTPException(status_code=404, detail="template not found")
            if data.get("owner_user_id") is None and _oid2(principal) is not None:
                raise HTTPException(status_code=404, detail="template not found")
        except HTTPException:
            raise
        except Exception:
            pass
    tpl_dir = Path(settings.templates_dir)
    archive = tpl_dir / f"{template_id}.tar.gz"
    meta = tpl_dir / f"{template_id}.json"
    if archive.exists():
        archive.unlink()
    if meta.exists():
        meta.unlink()


@authenticated.get("/router/catalog")
def router_catalog(request: Request) -> list[dict[str, Any]]:
    from agent_system.services.llm_catalog import DEFAULT_CATALOG

    return DEFAULT_CATALOG.to_json()


@authenticated.get("/router/roles")
def router_roles(request: Request) -> list[dict[str, Any]]:
    from agent_system.services.worker_roles import list_roles

    return [r.to_json() for r in list_roles()]


class RoutePreview(BaseModel):
    worker_role: str = ""
    task_type: str = "general"
    requires_tool_calling: bool = False
    min_context: int = 0
    requires_vision: bool = False
    min_coding: int = 0


@authenticated.post("/router/preview")
def router_preview(body: RoutePreview, request: Request) -> dict[str, Any]:
    from agent_system.services.llm_catalog import DEFAULT_CATALOG
    from agent_system.services.llm_router import RoutingRequest, rank_candidates

    req = RoutingRequest(
        task_type=body.task_type,
        worker_role=body.worker_role,
        requires_tool_calling=body.requires_tool_calling,
        min_context=body.min_context,
        requires_vision=body.requires_vision,
        min_coding=body.min_coding,
    )
    ranked = rank_candidates(req, DEFAULT_CATALOG, None, None)
    return {
        "candidates": [
            {"provider": c.provider, "model_id": c.model_id, "reason": reason}
            for c, reason in ranked[:8]
        ],
        "total": len(ranked),
    }


class SwarmPlanRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=4000)
    max_workers: int = 4
    task_type: str = Field(default="general", pattern=r"^[A-Za-z0-9_]{1,40}$")


@authenticated.post("/swarm/plan")
def swarm_plan(body: SwarmPlanRequest, request: Request) -> dict[str, Any]:
    from agent_system.services.swarm import plan_swarm

    settings = request.app.state.settings
    cap = min(int(body.max_workers or 1), int(getattr(settings, "swarm_max_workers", 4) or 4))
    return plan_swarm(body.goal, max_workers=cap, task_type=body.task_type).to_json()


@authenticated.get("/swarm/{master_task_id}")
def swarm_status(
    master_task_id: str,
    request: Request,
    principal: Annotated[Any | None, Depends(get_principal)],
) -> dict[str, Any]:
    from agent_system.services.swarm import verify_swarm

    factory = request.app.state.session_factory
    bus: EventBus = request.app.state.event_bus
    with session_scope(factory) as db:
        row = enforce_task_visible(db, master_task_id, principal)
        session_id = row.session_id
    summary = verify_swarm(factory, bus, master_task_id, session_id=session_id)
    return {"master_task_id": master_task_id, "summary": summary}


class SwarmCreate(BaseModel):
    session_id: str
    master_task_id: str
    goal: str = Field(min_length=1, max_length=4000)
    max_workers: int = 4
    task_type: str = Field(default="general", pattern=r"^[A-Za-z0-9_]{1,40}$")


@authenticated.post("/swarm", status_code=201)
def create_swarm_endpoint(
    body: SwarmCreate,
    request: Request,
    principal: Annotated[Any | None, Depends(get_principal)],
) -> dict[str, Any]:
    from agent_system.services.orchestrator import Supervisor
    from agent_system.services.swarm import create_swarm, plan_swarm

    factory = request.app.state.session_factory
    settings = request.app.state.settings
    bus: EventBus = request.app.state.event_bus
    if not bool(getattr(settings, "swarm_enabled", True)):
        raise HTTPException(status_code=403, detail="swarm disabled")
    with session_scope(factory) as db:
        enforce_task_visible(db, body.master_task_id, principal)
        enforce_session_visible(db, body.session_id, principal)
    owner = getattr(principal, "user_id", None)
    supervisor = Supervisor(bus)
    cap = min(int(body.max_workers or 1), int(getattr(settings, "swarm_max_workers", 4) or 4))
    decision = plan_swarm(body.goal, max_workers=cap, task_type=body.task_type)
    if not decision.use_swarm:
        raise HTTPException(status_code=409, detail="goal is single-agent; no swarm planned")
    worker_ids = create_swarm(
        bus, factory, body.session_id, body.master_task_id, decision, supervisor, owner
    )
    return {
        "master_task_id": body.master_task_id,
        "worker_task_ids": worker_ids,
        "decision": decision.to_json(),
    }


@authenticated.get("/tasks/{task_id}/attempts")
def task_attempts(
    task_id: str,
    request: Request,
    principal: Annotated[Any | None, Depends(get_principal)],
) -> list[dict[str, Any]]:
    factory = request.app.state.session_factory
    with session_scope(factory) as db:
        enforce_task_visible(db, task_id, principal)
        try:
            from agent_system.infra.models import WorkerAttempt

            rows = (
                db.query(WorkerAttempt)
                .filter(WorkerAttempt.task_id == task_id)
                .order_by(WorkerAttempt.attempt_no)
                .all()
            )
        except Exception:
            return []
        return [
            {
                "attempt_no": r.attempt_no,
                "worker_id": r.worker_id,
                "provider": r.provider,
                "model_id": r.model_id,
                "status": r.status,
                "error": r.error,
                "latency_ms": r.latency_ms,
                "tool_calls_made": r.tool_calls_made,
            }
            for r in rows
        ]
