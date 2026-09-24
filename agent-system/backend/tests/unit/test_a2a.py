"""P3 — Agent-to-agent (A2A) handoff.

Acceptance: one working external delegation round-trip (delegate ->
external agent -> result -> task.completed), gated behind a fresh
per-target approval, fully evented, off by default via A2A_ENABLED=false.
"""

from __future__ import annotations

import json as _json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_system.domain.tasks import TaskState
from agent_system.infra.db import make_engine, make_session_factory, session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Base, EventRow, Session, Task
from agent_system.services.a2a import (
    A2ADisabledError,
    A2AError,
    A2ANeedsApprovalError,
    A2AService,
    sign_envelope,
)
from agent_system.services.permissions import (
    DANGEROUS_SCOPES,
    ApprovalRequest,
    Decision,
    PermissionGate,
    Policy,
    Risk,
)


def _settings(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "a2a_enabled": False,
        "api_session_secret": "test-secret-please-ignore",
        "api_port": 18098,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def env(tmp_path: Path) -> Any:
    engine = make_engine(f"sqlite:///{tmp_path / 'a2a.db'}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    bus = EventBus()
    with session_scope(factory) as db:
        db.add(Session(id="ses_a2a", goal="delegated goal", status="ACTIVE"))
        db.add(
            Task(
                id="task_a2a",
                session_id="ses_a2a",
                task_type="general",
                title="delegated task",
                state=TaskState.RUNNING.value,
            )
        )
    yield factory, bus
    engine.dispose()


class FakeExternalAgent:
    """Minimal external agent: accepts the delegation, signs a result.

    Shares API_SESSION_SECRET out-of-band with the delegator (required for
    result-envelope signatures — documented in the developer guide).
    """

    def __init__(self, secret: str) -> None:
        self.secret = secret
        self.received: list[dict[str, Any]] = []
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                envelope = _json.loads(self.rfile.read(length) or b"{}")
                outer.received.append(envelope)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"accepted": true}')

            def log_message(self, *a: Any) -> None:
                pass

        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{port}/agent"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def signed_result(self, delegation_id: str, task_id: str) -> dict[str, Any]:
        from agent_system.domain.events import utcnow

        return sign_envelope(
            {
                "delegation_id": delegation_id,
                "task_id": task_id,
                "result": {"summary": "done by external agent"},
                "issued_at": utcnow().isoformat(),
            },
            self.secret,
        )


class TestA2AGating:
    def test_off_by_default(self, env: Any) -> None:
        factory, bus = env
        svc = A2AService(_settings(), PermissionGate(), factory, bus)
        with pytest.raises(A2ADisabledError):
            svc.delegate("task_a2a", "http://x/", {}, "http://cb/")

    def test_bare_scope_stays_default_deny(self) -> None:
        assert "a2a:delegate" in DANGEROUS_SCOPES
        gate = PermissionGate()
        record = gate.request(
            ApprovalRequest(
                requested_action="delegate everything",
                risk=Risk.CRITICAL,
                scope="a2a:delegate",
                requester="agent",
            )
        )
        assert record.decision == Decision.DENIED

    def test_needs_approval_makes_no_network_call(self, env: Any) -> None:
        factory, bus = env
        agent = FakeExternalAgent("test-secret-please-ignore")
        url = agent.start()
        try:
            svc = A2AService(_settings(a2a_enabled=True), PermissionGate(), factory, bus)
            with pytest.raises(A2ANeedsApprovalError) as exc_info:
                svc.delegate("task_a2a", url, {"goal": "x"}, "http://cb/")
            assert exc_info.value.approval_id
            assert agent.received == []
        finally:
            agent.stop()


class TestA2ARoundTrip:
    def test_delegate_to_result_to_task_completed(self, env: Any) -> None:
        factory, bus = env
        gate = PermissionGate()
        agent = FakeExternalAgent("test-secret-please-ignore")
        url = agent.start()
        try:
            svc = A2AService(_settings(a2a_enabled=True), gate, factory, bus)
            # First attempt mints the approval; approve that exact action.
            try:
                svc.delegate("task_a2a", url, {"goal": "x"}, "http://cb/")
                pytest.fail("expected A2ANeedsApprovalError")
            except A2ANeedsApprovalError as exc:
                approval_id = exc.approval_id
            host = url.split("://", 1)[1].split("/", 1)[0]
            record = gate.get(approval_id)
            assert record is not None
            # Rebind to the service's narrow scope, then approve once.
            record.scope = f"a2a:delegate:{host}"
            decided = gate.decide(approval_id, approve=True, policy=Policy.ALLOW_ONCE)
            assert decided.decision == Decision.APPROVED

            delegation = svc.delegate(
                "task_a2a", url, {"goal": "x"}, "http://cb/", approval_id=approval_id
            )
            assert delegation.status == "in_flight"
            assert len(agent.received) == 1
            assert agent.received[0]["body"]["delegation_id"] == delegation.delegation_id

            outcome = svc.handle_callback(agent.signed_result(delegation.delegation_id, "task_a2a"))
            assert outcome["status"] == "done"
            assert svc.get(delegation.delegation_id) is not None
            assert svc.get(delegation.delegation_id).status == "done"  # type: ignore[union-attr]

            with session_scope(factory) as db:
                task = db.get(Task, "task_a2a")
                assert task is not None
                assert task.state == TaskState.SUCCEEDED.value
                assert task.result_json == {"summary": "done by external agent"}
                types = [r.type for r in db.query(EventRow).all()]
            assert "a2a.delegated" in types
            assert "a2a.result" in types
            assert "task.completed" in types
        finally:
            agent.stop()

    def test_tampered_callback_rejected(self, env: Any) -> None:
        from agent_system.domain.events import utcnow

        factory, bus = env
        gate = PermissionGate()
        agent = FakeExternalAgent("test-secret-please-ignore")
        url = agent.start()
        try:
            svc = A2AService(_settings(a2a_enabled=True), gate, factory, bus)
            try:
                svc.delegate("task_a2a", url, {}, "http://cb/")
            except A2ANeedsApprovalError as exc:
                record = gate.get(exc.approval_id)
                assert record is not None
                record.scope = svc._scope_for(url)  # noqa: SLF001
                gate.decide(exc.approval_id, approve=True, policy=Policy.ALLOW_ONCE)
                delegation = svc.delegate(
                    "task_a2a", url, {}, "http://cb/", approval_id=exc.approval_id
                )
            good = agent.signed_result(delegation.delegation_id, "task_a2a")
            good["body"]["result"] = {"summary": "forged"}
            with pytest.raises(A2AError, match="[Ss]ignature"):
                svc.handle_callback(good)
            with pytest.raises(A2AError, match="unknown delegation"):
                svc.handle_callback(
                    sign_envelope(
                        {
                            "delegation_id": "a2a_nope",
                            "task_id": "task_a2a",
                            "result": {},
                            "issued_at": str(utcnow().isoformat()),
                        },
                        "test-secret-please-ignore",
                    )
                )
        finally:
            agent.stop()

    def test_refuses_non_http_agent_url(self, env: Any) -> None:
        factory, bus = env
        svc = A2AService(_settings(a2a_enabled=True), PermissionGate(), factory, bus)
        with pytest.raises(A2AError, match="non-HTTP"):
            svc.delegate("task_a2a", "ftp://evil/x", {}, "http://cb/")


class TestA2AEndpoints:
    """API wiring: disabled by default (503), never an open relay."""

    def test_callback_503_when_disabled(self) -> None:
        from fastapi.testclient import TestClient

        from agent_system.api.main import app

        with TestClient(app) as client:
            r = client.post("/api/v1/a2a/callback", json={"body": {}, "signature": "x"})
            assert r.status_code == 503

    def test_delegate_requires_auth_and_is_off(self) -> None:
        from fastapi.testclient import TestClient

        from agent_system.api.main import app

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/a2a/delegate",
                json={"task_id": "t", "agent_url": "http://x/", "callback_url": "http://c/"},
            )
            assert r.status_code == 401  # auth first, even when disabled
