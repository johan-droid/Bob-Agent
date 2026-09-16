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
    factory: Any, bus: EventBus, after_sequence: int, queue: asyncio.Queue[Any]
) -> int:
    """Replay persisted events after `after_sequence`; returns last seq sent."""
    last = after_sequence
    from agent_system.infra.db import session_scope

    with session_scope(factory) as db:
        for event in bus.replay_after(db, after_sequence=last, limit=1000):
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
    websocket: WebSocket, after_sequence: int = 0, token: str | None = None
) -> None:
    """WebSocket event stream: `?after_sequence=N` replays persisted history.

    Frame shapes: `{"kind": "event", ...}` or `{"kind": "heartbeat"}`.
    Must authenticate before the handshake completes — unauthenticated
    connections are rejected with code 1008.
    """
    if not _auth_from_ws(websocket, token):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    factory = websocket.app.state.session_factory
    bus: EventBus = websocket.app.state.event_bus
    queue: asyncio.Queue[tuple[str, dict[str, Any] | None]] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def on_event(event: Any) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, ("event", _event_dict(event)))

    bus.subscribe_all(on_event)
    try:
        # Replay history first so the client starts from a consistent point.
        last = await _drain(factory, bus, after_sequence, queue)

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
) -> StreamingResponse:
    """Server-Sent Events stream with the same replay-then-follow semantics.

    SSE comment lines (`: heartbeat`) keep proxies from closing idle streams.
    `max_events` bounds the stream for curl/testing; 0 (default) = unbounded.
    """

    async def generator() -> AsyncIterator[str]:
        factory = request.app.state.session_factory
        bus: EventBus = request.app.state.event_bus
        queue: asyncio.Queue[tuple[str, dict[str, Any] | None]] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def on_event(event: Any) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, ("event", _event_dict(event)))

        bus.subscribe_all(on_event)
        last = after_sequence
        sent = 0
        try:
            from agent_system.infra.db import session_scope

            with session_scope(factory) as db:
                for event in bus.replay_after(db, after_sequence=last, limit=1000):
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
