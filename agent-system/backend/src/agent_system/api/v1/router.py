"""API v1 routers (v3.1 §16).

Every endpoint: versioned path, typed request/response (Pydantic), documented
errors, auth. Sessions/tasks/approvals/events run on real services + SQLite.
"""

from __future__ import annotations

import threading as _threading
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from agent_system.api.deps import get_authenticator
from agent_system.config import get_settings
from agent_system.domain import ids
from agent_system.domain.events import Event, EventSensitivity, utcnow
from agent_system.domain.tasks import InvalidTransitionError, TaskState, validate_transition
from agent_system.infra.db import session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Artifact, Session, Task, Workspace
from agent_system.services.auth import Authenticator
from agent_system.services.permissions import (
    ApprovalRequest as GateRequest,
)
from agent_system.services.permissions import (
    Decision,
    PermissionGate,
    Policy,
    Risk,
)

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
def ready() -> dict[str, Any]:
    from agent_system.api.main import api_ready

    return api_ready()


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


def _check_token_rate_limit(request: Request) -> None:
    client_ip = request.client.host if request.client else "unknown"
    now = _time.monotonic()
    with _TOKEN_LOCK:
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
def create_session(body: SessionCreate, request: Request) -> SessionOut:
    factory = request.app.state.session_factory
    bus: EventBus = request.app.state.event_bus
    session_id = ids.new_session_id()
    with session_scope(factory) as db:
        db.add(Session(id=session_id, goal=body.goal, status="ACTIVE"))
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
def list_sessions(request: Request, limit: int = 50, offset: int = 0) -> list[SessionOut]:
    factory = request.app.state.session_factory
    with session_scope(factory) as db:
        rows = (
            db.query(Session).order_by(Session.created_at.desc()).offset(offset).limit(limit).all()
        )
        return [SessionOut(id=r.id, goal=r.goal, status=r.status) for r in rows]


@authenticated.get("/sessions/{session_id}")
def get_session(session_id: str, request: Request) -> SessionOut:
    factory = request.app.state.session_factory
    with session_scope(factory) as db:
        row = db.get(Session, session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="session not found")
        return SessionOut(id=row.id, goal=row.goal, status=row.status)


class SessionUpdate(BaseModel):
    goal: str | None = Field(default=None, min_length=1, max_length=10_000)
    status: str | None = None


@authenticated.patch("/sessions/{session_id}")
def update_session(session_id: str, body: SessionUpdate, request: Request) -> SessionOut:
    factory = request.app.state.session_factory
    bus: EventBus = request.app.state.event_bus
    with session_scope(factory) as db:
        row = db.get(Session, session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="session not found")
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


@authenticated.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: str, request: Request) -> None:
    factory = request.app.state.session_factory
    bus: EventBus = request.app.state.event_bus
    with session_scope(factory) as db:
        row = db.get(Session, session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="session not found")
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
    task_type: str = Field(pattern=r"^[a-z_]{1,40}$")
    title: str = Field(min_length=1, max_length=500)
    input: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
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


def _task_out(row: Task) -> TaskOut:
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
    )


@authenticated.post("/tasks", status_code=201)
def create_task(body: TaskCreate, request: Request) -> TaskOut:
    factory = request.app.state.session_factory
    bus: EventBus = request.app.state.event_bus
    # Idempotency (v3.1 §10):
    if body.idempotency_key:
        with session_scope(factory) as db:
            existing = db.query(Task).filter_by(idempotency_key=body.idempotency_key).first()
            if existing is not None:
                return _task_out(existing)
    task_id = ids.new_task_id()
    with session_scope(factory) as db:
        if db.get(Session, body.session_id) is None:
            raise HTTPException(status_code=404, detail="session not found")
        task = Task(
            id=task_id,
            session_id=body.session_id,
            task_type=body.task_type,
            title=body.title,
            input_json=body.input,
            depends_on_json=body.depends_on,
            agent_type=body.agent_type,
            idempotency_key=body.idempotency_key,
            state=TaskState.PENDING.value,
        )
        db.add(task)
        bus.emit(
            Event(type="task.created", session_id=body.session_id, task_id=task_id, actor="user"),
            db,
        )
    return _task_out(task)


@authenticated.get("/tasks")
def list_tasks(
    request: Request,
    session_id: str | None = None,
    state: str | None = None,
    limit: Annotated[int, Query(le=200)] = 50,
) -> list[TaskOut]:
    factory = request.app.state.session_factory
    with session_scope(factory) as db:
        query = db.query(Task)
        if session_id:
            query = query.filter_by(session_id=session_id)
        if state:
            query = query.filter_by(state=state.upper())
        rows = query.order_by(Task.created_at.desc()).limit(limit).all()
        return [_task_out(r) for r in rows]


@authenticated.get("/tasks/{task_id}")
def get_task(task_id: str, request: Request) -> TaskOut:
    factory = request.app.state.session_factory
    with session_scope(factory) as db:
        row = db.get(Task, task_id)
        if row is None:
            raise HTTPException(status_code=404, detail="task not found")
        return _task_out(row)


class TaskTransition(BaseModel):
    target: TaskState
    reason: str | None = None


@authenticated.post("/tasks/{task_id}/transition")
def transition_task(task_id: str, body: TaskTransition, request: Request) -> TaskOut:
    """Explicit, validated state transition — invalid ones are rejected (v3.1 §7)."""
    factory = request.app.state.session_factory
    bus: EventBus = request.app.state.event_bus
    with session_scope(factory) as db:
        row = db.get(Task, task_id)
        if row is None:
            raise HTTPException(status_code=404, detail="task not found")
        current = TaskState(row.state)
        try:
            validate_transition(current, body.target)
        except InvalidTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        row.state = body.target.value
        # attempt counts "times execution started" — increment on every entry
        # into RUNNING (manual operator transitions included).
        if body.target == TaskState.RUNNING:
            row.attempt += 1
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
        return _task_out(row)


@authenticated.post("/tasks/{task_id}/retry", status_code=202)
def retry_task(task_id: str, request: Request) -> TaskOut:
    factory = request.app.state.session_factory
    bus: EventBus = request.app.state.event_bus
    with session_scope(factory) as db:
        row = db.get(Task, task_id)
        if row is None:
            raise HTTPException(status_code=404, detail="task not found")
        current = TaskState(row.state)
        try:
            validate_transition(current, TaskState.QUEUED)
        except InvalidTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        row.state = TaskState.QUEUED.value
        # attempt counts "times execution started" — incremented only at the
        # RUNNING transition (worker/orchestrator), never on requeue.
        bus.emit(
            Event(
                type="task.queued",
                task_id=row.id,
                actor="user",
                payload={"retry": True, "attempt": row.attempt},
            ),
            db,
        )
        return _task_out(row)


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
def request_approval(body: ApprovalCreate, request: Request) -> ApprovalOut:
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
def list_approvals(request: Request, pending_only: bool = True) -> list[ApprovalOut]:
    gate: PermissionGate = request.app.state.gate
    records = gate.list_pending() if pending_only else gate.list_all()
    return [_approval_out(r) for r in records]


@authenticated.post("/approvals/{approval_id}/decision")
def decide_approval(approval_id: str, body: ApprovalDecision, request: Request) -> ApprovalOut:
    gate: PermissionGate = request.app.state.gate
    bus: EventBus = request.app.state.event_bus
    factory = request.app.state.session_factory
    try:
        record = gate.decide(
            approval_id, approve=body.approve, policy=body.policy, reason=body.reason
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
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
def sweep_approvals(request: Request) -> dict[str, object]:
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
def create_workspace(body: dict[str, Any], request: Request) -> WorkspaceOut:
    from agent_system.services.workspaces import WorkspaceManager

    factory = request.app.state.session_factory
    bus: EventBus = request.app.state.event_bus
    settings = request.app.state.settings
    ws_id = ids.new_workspace_id()
    name = str(body.get("name") or ws_id)[:100]
    manager = WorkspaceManager(settings.workspaces_dir)
    manager.create(ws_id)
    with session_scope(factory) as db:
        db.add(Workspace(id=ws_id, name=name, status="CREATED"))
        bus.emit(
            Event(type="workspace.created", actor="user", payload={"workspace_id": ws_id}),
            db,
        )
    return WorkspaceOut(id=ws_id, name=name, status="CREATED", size_bytes=0, file_count=0)


@authenticated.get("/workspaces")
def list_workspaces(request: Request) -> list[WorkspaceOut]:
    from agent_system.services.workspaces import WorkspaceManager

    factory = request.app.state.session_factory
    settings = request.app.state.settings
    manager = WorkspaceManager(settings.workspaces_dir)
    with session_scope(factory) as db:
        rows = db.query(Workspace).order_by(Workspace.created_at.desc()).all()
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
def workspace_tree(workspace_id: str, request: Request) -> dict[str, Any]:
    from agent_system.services.workspaces import WorkspaceManager

    factory = request.app.state.session_factory
    settings = request.app.state.settings
    manager = WorkspaceManager(settings.workspaces_dir)
    with session_scope(factory) as db:
        if db.get(Workspace, workspace_id) is None:
            raise HTTPException(status_code=404, detail="workspace not found")
    try:
        files = manager.tree(workspace_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"workspace_id": workspace_id, "files": files}


@authenticated.get("/workspaces/{workspace_id}/file")
def read_workspace_file(workspace_id: str, request: Request, path: str) -> dict[str, Any]:
    import base64

    from agent_system.services.workspaces import WorkspaceManager

    settings = request.app.state.settings
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
    workspace_id: str, body: WorkspaceWrite, request: Request
) -> dict[str, Any]:
    import base64

    from agent_system.services.workspaces import WorkspaceManager

    factory = request.app.state.session_factory
    bus: EventBus = request.app.state.event_bus
    settings = request.app.state.settings
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
def workspace_fingerprint(workspace_id: str, request: Request) -> dict[str, str]:
    from agent_system.services.workspaces import WorkspaceManager

    settings = request.app.state.settings
    manager = WorkspaceManager(settings.workspaces_dir)
    try:
        return {"workspace_id": workspace_id, "fingerprint": manager.fingerprint(workspace_id)}
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@authenticated.delete("/workspaces/{workspace_id}", status_code=204)
def delete_workspace(workspace_id: str, request: Request) -> None:
    from agent_system.services.workspaces import WorkspaceManager

    factory = request.app.state.session_factory
    bus: EventBus = request.app.state.event_bus
    settings = request.app.state.settings
    manager = WorkspaceManager(settings.workspaces_dir)
    with session_scope(factory) as db:
        if db.get(Workspace, workspace_id) is None:
            raise HTTPException(status_code=404, detail="workspace not found")
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
def sandbox_exec(workspace_id: str, body: SandboxExec, request: Request) -> dict[str, Any]:
    from agent_system.services.sandbox import DockerSandbox, SandboxUnavailableError
    from agent_system.services.workspaces import WorkspaceManager

    bus: EventBus = request.app.state.event_bus
    factory = request.app.state.session_factory
    settings = request.app.state.settings
    with session_scope(factory) as db:
        if db.get(Workspace, workspace_id) is None:
            raise HTTPException(status_code=404, detail="workspace not found")
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
        str(ws_path), body.command, timeout_seconds=body.timeout_seconds, network=body.network
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
def list_artifacts(request: Request, task_id: str | None = None) -> list[ArtifactOut]:
    factory = request.app.state.session_factory
    with session_scope(factory) as db:
        query = db.query(Artifact)
        if task_id:
            query = query.filter_by(task_id=task_id)
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
    after_sequence: int = 0,
    type: str | None = None,
    session_id: str | None = None,
    limit: Annotated[int, Query(le=500)] = 200,
) -> list[EventOut]:
    factory = request.app.state.session_factory
    bus: EventBus = request.app.state.event_bus
    with session_scope(factory) as db:
        # session_id is pushed into the SQL WHERE clause (indexed) instead
        # of Python post-filtering; limit is capped at 500 rows per page.
        events = bus.replay_after(
            db, after_sequence=after_sequence, event_type=type, limit=limit,
            session_id=session_id,
        )
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
    layer: str | None = None,
    search: str | None = None,
) -> list[VaultNoteOut]:
    from agent_system.services.memory import MemoryLayer, ObsidianVaultWriter

    settings = request.app.state.settings
    writer = ObsidianVaultWriter(Path(settings.vault_path))
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
                    links=[str(l) for l in fm.get("links") or []],
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
) -> VaultNoteDetail:
    from agent_system.services.memory import ObsidianVaultWriter

    settings = request.app.state.settings
    writer = ObsidianVaultWriter(Path(settings.vault_path))
    target = (writer.root / path).resolve()
    if not str(target).startswith(str(writer.root.resolve())):
        raise HTTPException(status_code=400, detail="invalid path")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="note not found")
    data = writer.read_note(target)
    fm = data.get("frontmatter") or {}
    rel_path = str(target.relative_to(writer.root))
    return VaultNoteDetail(
        name=target.name,
        path=rel_path,
        layer=str(fm.get("layer") or target.parent.name.upper()),
        title=str(fm.get("title") or target.stem),
        source=str(fm.get("source") or "unknown"),
        created=str(fm.get("created") or ""),
        tags=[str(t) for t in fm.get("tags") or []],
        links=[str(l) for l in fm.get("links") or []],
        size_bytes=target.stat().st_size,
        task_id=fm.get("task_id"),
        session_id=fm.get("session_id"),
        body=str(data.get("body") or ""),
    )


@authenticated.post("/vault/notes", status_code=201)
def create_vault_note(body: VaultNoteCreate, request: Request) -> VaultNoteDetail:
    from agent_system.services.memory import MemoryLayer, NoteMeta, ObsidianVaultWriter

    settings = request.app.state.settings
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
def delete_vault_note(request: Request, path: str) -> None:
    from agent_system.services.memory import ObsidianVaultWriter

    settings = request.app.state.settings
    writer = ObsidianVaultWriter(Path(settings.vault_path))
    target = (writer.root / path).resolve()
    if not str(target).startswith(str(writer.root.resolve())):
        raise HTTPException(status_code=400, detail="invalid path")
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
    from agent_system.services.workspaces import TemplateManager

    settings = request.app.state.settings
    tpl_dir = Path(settings.templates_dir)
    tpl_dir.mkdir(parents=True, exist_ok=True)
    out: list[TemplateOut] = []
    for archive in sorted(tpl_dir.glob("*.tar.gz")):
        tpl_id = archive.name[:-7]
        meta_file = tpl_dir / f"{tpl_id}.json"
        name = tpl_id
        skipped: list[str] = []
        created_str = datetime.fromtimestamp(archive.stat().st_mtime, tz=timezone.utc).isoformat()
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
def create_template(body: TemplateCreate, request: Request) -> TemplateOut:
    import json
    from agent_system.services.workspaces import TemplateManager, WorkspaceManager

    settings = request.app.state.settings
    ws_mgr = WorkspaceManager(settings.workspaces_dir)
    ws_path = ws_mgr.path(body.workspace_id)
    if not ws_path.exists():
        raise HTTPException(status_code=404, detail="workspace not found")

    tpl_mgr = TemplateManager(Path(settings.templates_dir))
    tpl_id = ids.new_id("tpl_")
    archive = tpl_mgr.snapshot(tpl_id, ws_path, body.name)
    meta_file = Path(settings.templates_dir) / f"{tpl_id}.json"
    now_str = datetime.now(timezone.utc).isoformat()
    meta_file.write_text(
        json.dumps(
            {
                "name": body.name,
                "workspace_id": body.workspace_id,
                "created_at": now_str,
                "skipped": tpl_mgr.last_skipped,
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
def restore_template(template_id: str, body: TemplateRestore, request: Request) -> WorkspaceOut:
    from agent_system.services.workspaces import TemplateManager, WorkspaceManager

    factory = request.app.state.session_factory
    bus: EventBus = request.app.state.event_bus
    settings = request.app.state.settings
    ws_id = ids.new_workspace_id()
    name = str(body.workspace_name or f"restored-{template_id[:8]}")[:100]

    ws_mgr = WorkspaceManager(settings.workspaces_dir)
    target_path = ws_mgr.create(ws_id)

    tpl_mgr = TemplateManager(Path(settings.templates_dir))
    try:
        restored_files = tpl_mgr.clone(template_id, target_path)
    except Exception as e:
        ws_mgr.delete(ws_id)
        raise HTTPException(status_code=400, detail=str(e))

    with session_scope(factory) as db:
        db.add(Workspace(id=ws_id, name=name, status="CREATED"))
        bus.emit(
            Event(
                type="workspace.restored",
                actor="user",
                payload={"workspace_id": ws_id, "template_id": template_id, "files": len(restored_files)},
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
def delete_template(template_id: str, request: Request) -> None:
    settings = request.app.state.settings
    tpl_dir = Path(settings.templates_dir)
    archive = tpl_dir / f"{template_id}.tar.gz"
    meta = tpl_dir / f"{template_id}.json"
    if archive.exists():
        archive.unlink()
    if meta.exists():
        meta.unlink()

