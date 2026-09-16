"""Realtime authentication rejection contracts."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from agent_system.api.main import app


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(app) as connection:
        yield connection


@pytest.mark.parametrize("authorization", [None, "Bearer invalid"])
def test_realtime_rejects_unauthenticated_clients(client: TestClient, authorization) -> None:
    from starlette.websockets import WebSocketDisconnect

    client.headers.pop("Authorization", None)
    if authorization:
        client.headers["Authorization"] = authorization
    assert client.get("/api/v1/events/latest-sequence").status_code == 401
    assert client.get("/api/v1/events/stream?max_events=1").status_code == 401
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/v1/ws/events"):
            pytest.fail("unauthenticated websocket was accepted")
    assert exc.value.code == 1008
