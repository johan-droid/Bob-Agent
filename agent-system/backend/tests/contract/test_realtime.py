"""Contract tests for realtime transports (WS + SSE) — v3.1 §17.

Covers: replay-from-sequence on connect, live delivery after replay,
and the latest-sequence endpoint. Uses the app lifespan (dev SQLite) like
the other contract tests.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from agent_system.api.main import app
from agent_system.domain.events import Event
from agent_system.infra.db import session_scope
from agent_system.infra.event_bus import EventBus


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        token = c.app.state.authenticator.bootstrap_token  # type: ignore[attr-defined]
        c.headers["Authorization"] = f"Bearer {token}"
        yield c


def _emit(client: TestClient, n: int) -> None:
    bus: EventBus = client.app.state.event_bus
    factory = client.app.state.session_factory
    with session_scope(factory) as db:
        for i in range(n):
            bus.emit(Event(type="session.created", actor="test", payload={"i": i}), db)


def test_latest_sequence_monotonic(client: TestClient) -> None:
    before = client.get("/api/v1/events/latest-sequence").json()["sequence"]
    _emit(client, 2)
    after = client.get("/api/v1/events/latest-sequence").json()["sequence"]
    assert after == before + 2


def test_events_endpoint_resume(client: TestClient) -> None:
    _emit(client, 2)
    before = client.get("/api/v1/events/latest-sequence").json()["sequence"]
    _emit(client, 2)
    resumed = client.get(f"/api/v1/events?after_sequence={before}").json()
    assert len(resumed) == 2
    assert all(e["sequence"] > before for e in resumed)


def test_sse_replays_history(client: TestClient) -> None:
    _emit(client, 3)
    latest = client.get("/api/v1/events/latest-sequence").json()["sequence"]
    with client.stream(
        "GET",
        f"/api/v1/events/stream?after_sequence={latest - 3}&max_events=3",
        timeout=10.0,
    ) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        body = b""
        for chunk in resp.iter_bytes():
            body += chunk
            if body.count(b"data:") >= 3:
                break
    text = body.decode()
    assert "session.created" in text
    lines = [ln for ln in text.splitlines() if ln.startswith("data: ")]
    seqs = [json.loads(ln[6:])["sequence"] for ln in lines[:3]]
    assert seqs == [latest - 2, latest - 1, latest]


def test_ws_replay_and_live(client: TestClient) -> None:
    _emit(client, 2)
    latest = client.get("/api/v1/events/latest-sequence").json()["sequence"]
    with client.websocket_connect(f"/api/v1/ws/events?after_sequence={latest - 2}") as ws:
        got: list[dict] = []
        for _ in range(2):
            msg = ws.receive_json()
            if msg.get("kind") == "event":
                got.append(msg)
        assert len(got) == 2
        assert got[0]["sequence"] == latest - 1
        # live event emitted after connect arrives too
        bus: EventBus = client.app.state.event_bus
        factory = client.app.state.session_factory
        with session_scope(factory) as db:
            bus.emit(Event(type="session.created", actor="live"), db)
        msg = ws.receive_json()
        assert msg["kind"] == "event"
        assert msg["actor"] == "live"
