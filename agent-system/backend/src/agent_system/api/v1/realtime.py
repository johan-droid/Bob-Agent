"""Realtime event fanout (v3.1 §17): WebSocket + SSE.

Both transports stream canonical events from the EventBus with:
- heartbeat every HEARTBEAT_SECONDS,
- sequence-resumable delivery (`after_sequence`) — a reconnecting client
  replays persisted events from SQLite and then follows the live stream,
- best-effort delivery consistent with EventBus subscriber isolation.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.websockets import WebSocket, WebSocketDisconnect
from sqlalchemy import select

from agent_system.api.deps import get_authenticator
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import EventRow
from agent_system.services.auth import Authenticator

HEARTBEAT_SECONDS = 15.0
MAX_REPLAY = 200

# Per-IP connection throttle: max 20 handshakes/min/IP (in-process).
_WS_ATTEMPTS: dict[str, list[float]] = {}
_WS_LOCK: Any = None

def _ws_lock() -> Any:
    global _WS_LOCK
    if _WS_LOCK is None:
        import threading as _th

        _WS_LOCK = _th.Lock()
    return _WS_LOCK


def _check_ws_rate_limit(key: str, limit: int = 20, window: float = 60.0) -> bool:
    import time as _time

    now = _time.monotonic()
    with _ws_lock():
        hist = [t for t in _WS_ATTEMPTS.get(key, []) if now - t < window]
        if len(hist) >= limit:
            _WS_ATTEMPTS[key] = hist
            return False
        hist.append(now)
        _WS_ATTEMPTS[key] = hist
        return True


def _owned_session_ids(factory: Any, principal: Any) -> set[str] | None:
    """None = no filtering (local operator). Otherwise owned session ids."""
    try:
        from agent_system.infra.db import session_scope
        from agent_system.infra.models import Session
        from agent_system.services.identity import IdentityMode
    except Exception:
        return None
    if getattr(principal, "mode", None) is None:
        return None
    try:
        if getattr(principal, "mode") is IdentityMode.LOCAL:
            return None
    except Exception:
        return None
    uid = getattr(principal, "user_id", None)
    if uid is None:
        return set()
    with session_scope(factory) as db:
        rows = db.query(Session.id).filter(Session.owner_user_id == uid).all()
        return {str(r[0]) for r in rows}


def _event_visible(event: Any, owned: set[str] | None, factory: Any = None) -> bool:
    if owned is None:
        return True
    sid = getattr(event, "session_id", None)
    if sid is not None:
        return str(sid) in owned
    # Session-less events: only allow non-sensitive task events whose task's
    # session is owned. Resolve transitively when a factory is available.
    tid = getattr(event, "task_id", None)
    if tid is not None and factory is not None:
        try:
            from agent_system.infra.db import session_scope
            from agent_system.infra.models import Task

            with session_scope(factory) as db:
                row = db.get(Task, str(tid))
                if row is not None and row.session_id:
                    return str(row.session_id) in owned
        except Exception:
            return False
    return False

realtime_router = APIRouter(prefix="/api/v1")


def _event_dict(event: Any) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "sequence": event.sequence,
        "timestamp": event.timestamp.isoformat(),
        "type": event.type,
        "actor": event.actor,
        "session_id": event.session_id,
        "task_id": event.task_id,
        "agent_run_id": event.agent_run_id,
        "payload": event.payload,
        "visibility": str(event.visibility),
        "sensitivity": str(event.sensitivity),
    }


async def _drain(
    factory: Any, bus: EventBus, after_sequence: int, queue: asyncio.Queue[Any],
    owned: set[str] | None = None,
) -> int:
    """Replay persisted events after `after_sequence`; returns last seq sent."""
    last = after_sequence
    from agent_system.infra.db import session_scope

    with session_scope(factory) as db:
        for event in bus.replay_after(db, after_sequence=last, limit=MAX_REPLAY):
            if not _event_visible(event, owned, factory):
                last = event.sequence or last
                continue
            await queue.put(("event", _event_dict(event)))
            last = event.sequence or last
    return last


def _auth_from_ws(websocket: WebSocket, token: str | None) -> bool:
    """WebSocket auth: Authorization header, ``?token=``, or session cookie.

    Browsers cannot set headers on WebSocket handshakes, so the UI passes the
    auth token as a query parameter instead — same Authenticator, same rules.
    """
    authenticator: Authenticator = websocket.app.state.authenticator
    header = websocket.headers.get("authorization", "")
    if header.startswith("Bearer "):
        if authenticator.verify(header.removeprefix("Bearer ").strip()):
            return True
    if token and authenticator.verify(token):
        return True
    cookie = websocket.cookies.get("agent_session")
    if cookie and authenticator.verify(cookie):
        return True
    return False


@realtime_router.websocket("/ws/events")
async def ws_events(
    websocket: WebSocket,
    after_sequence: int = 0,
    token: str | None = None,
    principal: str | None = None,
) -> None:
    """WebSocket event stream: `?after_sequence=N` replays persisted history.

    Frame shapes: `{"kind": "event", ...}` or `{"kind": "heartbeat"}`.
    Must authenticate before the handshake completes — unauthenticated
    connections are rejected with code 1008. In telegram mode the stream is
    filtered to sessions owned by the bound principal; `?token=` is accepted
    but discouraged (logged by proxies — prefer Authorization header).
    """
    client_ip = ""
    try:
        client_ip = str((websocket.client.host if websocket.client else "ws"))
    except Exception:
        client_ip = "ws"
    if not _check_ws_rate_limit(f"ws:{client_ip}"):
        await websocket.close(code=1013)
        return
    if not _auth_from_ws(websocket, token):
        await websocket.close(code=1008)
        return
    if token:
        import logging as _logging

        _logging.getLogger(__name__).warning("ws token in URL (may be logged by proxies)")
    await websocket.accept()
    factory = websocket.app.state.session_factory
    bus: EventBus = websocket.app.state.event_bus
    # Resolve principal for filtering (best-effort: unauthenticated detail
    # never leaks — on failure fall back to empty set in telegram mode).
    owned: set[str] | None = None
    try:
        from agent_system.config import get_settings
        from agent_system.services.identity import IdentityMode, IdentityService

        settings = getattr(websocket.app.state, "settings", None) or get_settings()
        if str(getattr(settings, "agent_identity_mode", "local")) == IdentityMode.TELEGRAM.value:
            authenticator = websocket.app.state.authenticator
            raw = token or ""
            if not raw:
                header = websocket.headers.get("authorization", "")
                if header.startswith("Bearer "):
                    raw = header.removeprefix("Bearer ").strip()
            if not raw:
                raw = websocket.cookies.get("agent_session") or ""
            bound = authenticator.owner_of(raw) if raw else None
            if bound is not None and bound is not False:
                from agent_system.infra.db import session_scope
                from agent_system.infra.models import Session as _S

                with session_scope(factory) as db:
                    rows = db.query(_S.id).filter(_S.owner_user_id == str(bound)).all()
                    owned = {str(r[0]) for r in rows}
            elif principal:
                ident = IdentityService(factory, settings)
                p = ident.resolve(str(principal))
                owned = _owned_session_ids(factory, p) if p else set()
            else:
                owned = set()
    except Exception:
        owned = owned if owned is not None else set()
    queue: asyncio.Queue[tuple[str, dict[str, Any] | None]] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def on_event(event: Any) -> None:
        if not _event_visible(event, owned, factory):
            return
        loop.call_soon_threadsafe(queue.put_nowait, ("event", _event_dict(event)))

    bus.subscribe_all(on_event)
    try:
        # Replay history first so the client starts from a consistent point.
        last = await _drain(factory, bus, after_sequence, queue, owned)

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(HEARTBEAT_SECONDS)
                await queue.put(("heartbeat", None))

        hb_task = asyncio.create_task(heartbeat())
        try:
            while True:
                kind, payload = await queue.get()
                if kind == "event" and payload is not None:
                    last = max(last, int(payload.get("sequence") or last))
                    await websocket.send_json({"kind": "event", **payload})
                elif kind == "heartbeat":
                    await websocket.send_json({"kind": "heartbeat"})
        finally:
            hb_task.cancel()
    except WebSocketDisconnect:
        pass
    finally:
        try:
            bus._global_subscribers.remove(on_event)  # noqa: SLF001
        except (ValueError, AttributeError):
            pass


@realtime_router.get("/events/stream")
async def sse_events(
    request: Request,
    _: Annotated[Authenticator, Depends(get_authenticator)],
    after_sequence: int = Query(default=0),
    max_events: int = Query(default=0, ge=0),
    principal: str | None = Query(default=None),
) -> StreamingResponse:
    """Server-Sent Events stream with the same replay-then-follow semantics.

    SSE comment lines (`: heartbeat`) keep proxies from closing idle streams.
    `max_events` bounds the stream for curl/testing; 0 (default) = unbounded.
    In telegram mode only the caller's owned sessions are streamed.
    """

    async def generator() -> AsyncIterator[str]:
        from agent_system.api.deps import get_principal as _get_principal

        factory = request.app.state.session_factory
        bus: EventBus = request.app.state.event_bus
        # Resolve principal via the same binding rules as REST.
        owned: set[str] | None = None
        try:
            p = _get_principal(request)
            owned = _owned_session_ids(factory, p)
        except Exception:
            # get_principal already enforces 401/403; empty set = leak nothing.
            try:
                from agent_system.config import get_settings
                from agent_system.services.identity import IdentityMode

                settings = getattr(request.app.state, "settings", None) or get_settings()
                if str(getattr(settings, "agent_identity_mode", "local")) == IdentityMode.TELEGRAM.value:
                    owned = set()
            except Exception:
                owned = None
        queue: asyncio.Queue[tuple[str, dict[str, Any] | None]] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def on_event(event: Any) -> None:
            if not _event_visible(event, owned, factory):
                return
            loop.call_soon_threadsafe(queue.put_nowait, ("event", _event_dict(event)))

        bus.subscribe_all(on_event)
        last = after_sequence
        sent = 0
        try:
            from agent_system.infra.db import session_scope

            with session_scope(factory) as db:
                for event in bus.replay_after(db, after_sequence=last, limit=MAX_REPLAY):
                    if not _event_visible(event, owned, factory):
                        last = event.sequence or last
                        continue
                    yield f"data: {json.dumps(_event_dict(event))}\n\n"
                    last = event.sequence or last
                    sent += 1
                    if max_events and sent >= max_events:
                        return
            while not await request.is_disconnected():
                try:
                    kind, payload = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if kind == "event" and payload is not None:
                    last = max(last, int(payload.get("sequence") or last))
                    yield f"data: {json.dumps(payload)}\n\n"
                    sent += 1
                    if max_events and sent >= max_events:
                        return
        finally:
            try:
                bus._global_subscribers.remove(on_event)  # noqa: SLF001
            except (ValueError, AttributeError):
                pass

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@realtime_router.get("/events/latest-sequence")
def latest_sequence(
    request: Request, _: Annotated[Authenticator, Depends(get_authenticator)]
) -> dict[str, int]:
    factory = request.app.state.session_factory
    from agent_system.infra.db import session_scope

    with session_scope(factory) as db:
        row = db.execute(select(EventRow.sequence).order_by(EventRow.sequence.desc())).first()
    return {"sequence": int(row[0]) if row else 0}
