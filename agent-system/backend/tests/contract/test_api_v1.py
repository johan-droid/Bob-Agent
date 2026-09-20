"""Contract tests — API v1 (v3.1 §16, Phase 3 acceptance)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from agent_system.api.main import app
from agent_system.config import get_settings


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(app) as client:
        token = client.app.state.authenticator.bootstrap_token  # type: ignore[attr-defined]
        client.headers["Authorization"] = f"Bearer {token}"
        yield client


class TestAuth:
    def test_health_open_ready_reports_db(self, client: TestClient) -> None:
        assert client.get("/api/v1/health").status_code == 200
        body = client.get("/api/v1/ready").json()
        assert body["status"] in {"ok", "degraded", "not_ready"}
        assert "database" in body["checks"]

    def test_protected_endpoint_requires_auth(self) -> None:
        with TestClient(app) as c:
            # No Authorization header:
            r = c.get("/api/v1/sessions")
            assert r.status_code == 401

    def test_bad_token_rejected(self) -> None:
        with TestClient(app) as c:
            c.headers["Authorization"] = "Bearer nope"
            assert c.get("/api/v1/sessions").status_code == 401

    def test_token_mint_requires_secret(self, client: TestClient) -> None:
        r = client.post("/api/v1/auth/token", json={"session_secret": "wrong"})
        assert r.status_code == 403
        r = client.post(
            "/api/v1/auth/token",
            json={"session_secret": get_settings().agent_bootstrap_secret},
        )
        assert r.status_code == 200
        assert "token" in r.json()


class TestSessions:
    def test_create_and_get(self, client: TestClient) -> None:
        r = client.post("/api/v1/sessions", json={"goal": "fix failing tests"})
        assert r.status_code == 201
        session_id = r.json()["id"]
        assert session_id.startswith("ses_")
        got = client.get(f"/api/v1/sessions/{session_id}")
        assert got.status_code == 200
        assert got.json()["goal"] == "fix failing tests"

    def test_update_and_delete_session(self, client: TestClient) -> None:
        r = client.post("/api/v1/sessions", json={"goal": "initial goal"})
        assert r.status_code == 201
        session_id = r.json()["id"]

        # Update
        patch_r = client.patch(f"/api/v1/sessions/{session_id}", json={"goal": "updated goal"})
        assert patch_r.status_code == 200
        assert patch_r.json()["goal"] == "updated goal"

        # Delete
        del_r = client.delete(f"/api/v1/sessions/{session_id}")
        assert del_r.status_code == 204

        # 404 on get
        assert client.get(f"/api/v1/sessions/{session_id}").status_code == 404

    def test_missing_session_404(self, client: TestClient) -> None:
        assert client.get("/api/v1/sessions/ses_missing").status_code == 404

    def test_invalid_goal_422(self, client: TestClient) -> None:
        assert client.post("/api/v1/sessions", json={"goal": ""}).status_code == 422


class TestTasks:
    def test_create_and_transition_lifecycle(self, client: TestClient) -> None:
        session_id = client.post("/api/v1/sessions", json={"goal": "g"}).json()["id"]
        r = client.post(
            "/api/v1/tasks",
            json={"session_id": session_id, "task_type": "code", "title": "fix"},
        )
        assert r.status_code == 201
        task = r.json()
        assert task["id"].startswith("task_")
        assert task["state"] == "PENDING"

        # Valid walk:
        for target in ("PLANNING", "QUEUED", "RUNNING"):
            t = client.post(f"/api/v1/tasks/{task['id']}/transition", json={"target": target})
            assert t.status_code == 200, t.text
            assert t.json()["state"] == target

        # Invalid jump rejected with 409:
        t = client.post(f"/api/v1/tasks/{task['id']}/transition", json={"target": "PENDING"})
        assert t.status_code == 409

        t = client.post(f"/api/v1/tasks/{task['id']}/transition", json={"target": "SUCCEEDED"})
        assert t.status_code == 200

    def test_idempotency_key_returns_same_task(self, client: TestClient) -> None:
        session_id = client.post("/api/v1/sessions", json={"goal": "g"}).json()["id"]
        body = {
            "session_id": session_id,
            "task_type": "code",
            "title": "once",
            "idempotency_key": "op:xyz",
        }
        first = client.post("/api/v1/tasks", json=body).json()
        second = client.post("/api/v1/tasks", json=body).json()
        assert first["id"] == second["id"]

    def test_task_for_missing_session_404(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/tasks", json={"session_id": "ses_none", "task_type": "code", "title": "x"}
        )
        assert r.status_code == 404

    def test_retry(self, client: TestClient) -> None:
        session_id = client.post("/api/v1/sessions", json={"goal": "g"}).json()["id"]
        task_id = client.post(
            "/api/v1/tasks", json={"session_id": session_id, "task_type": "code", "title": "x"}
        ).json()["id"]
        client.post(f"/api/v1/tasks/{task_id}/transition", json={"target": "QUEUED"})
        client.post(f"/api/v1/tasks/{task_id}/transition", json={"target": "RUNNING"})
        client.post(f"/api/v1/tasks/{task_id}/transition", json={"target": "FAILED"})
        r = client.post(f"/api/v1/tasks/{task_id}/retry")
        assert r.status_code == 202
        assert r.json()["state"] == "QUEUED"
        # attempt counts "times execution started": it was incremented once at
        # the RUNNING transition and is NOT incremented again on requeue.
        assert r.json()["attempt"] == 1


class TestApprovals:
    def test_request_and_approve(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/approvals",
            json={
                "requested_action": "fs.delete",
                "risk": "MEDIUM",
                "scope": "file:delete",
                "requester": "CodeAgent",
            },
        )
        assert r.status_code == 202
        approval_id = r.json()["approval_id"]
        assert r.json()["decision"] == "PENDING"

        d = client.post(
            f"/api/v1/approvals/{approval_id}/decision",
            json={"approve": True, "policy": "ALLOW_ONCE"},
        )
        assert d.status_code == 200
        assert d.json()["decision"] == "APPROVED"

    def test_dangerous_scope_auto_denied(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/approvals",
            json={
                "requested_action": "submit payment",
                "risk": "CRITICAL",
                "scope": "browser:transact",
                "requester": "BrowserAgent",
            },
        )
        assert r.json()["decision"] == "DENIED"

    def test_pending_listing(self, client: TestClient) -> None:
        client.post(
            "/api/v1/approvals",
            json={
                "requested_action": "net.fetch",
                "risk": "LOW",
                "scope": "net:fetch",
                "requester": "ResearchAgent",
            },
        )
        pending = client.get("/api/v1/approvals?pending_only=true").json()
        assert any(p["scope"] == "net:fetch" for p in pending)


class TestEvents:
    def test_events_persisted_and_resumable(self, client: TestClient) -> None:
        session_id = client.post("/api/v1/sessions", json={"goal": "events"}).json()["id"]
        client.post(
            "/api/v1/tasks", json={"session_id": session_id, "task_type": "code", "title": "t"}
        )
        all_events = client.get("/api/v1/events?limit=100").json()
        assert any(e["type"] == "session.created" for e in all_events)
        assert any(e["type"] == "task.created" for e in all_events)
        assert all_events == sorted(all_events, key=lambda e: e["sequence"])

        mid = all_events[-1]["sequence"]
        tail = client.get(f"/api/v1/events?after_sequence={mid}").json()
        assert all(e["sequence"] > mid for e in tail)
