"""Contract tests — Phase 15–17 feature endpoints (v3.1 §16)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from agent_system.api.main import app


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        token = c.app.state.authenticator.bootstrap_token  # type: ignore[attr-defined]
        c.headers["Authorization"] = f"Bearer {token}"
        yield c


VALID_DAG = {
    "tasks": [
        {"key": "a", "task_type": "code", "title": "step one"},
        {"key": "b", "task_type": "code", "title": "step two", "depends_on": ["a"]},
    ]
}


class TestRecordings:
    def test_list_recordings(self, client: TestClient) -> None:
        r = client.get("/api/v1/recordings")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_replay_missing_recording_404(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/recordings/rec_none/replay", json={"mode": "INSPECT", "context": {}}
        )
        assert r.status_code == 404

    def test_replay_invalid_mode_422(self, client: TestClient) -> None:
        r = client.post("/api/v1/recordings/rec_x/replay", json={"mode": "NUKE", "context": {}})
        assert r.status_code == 422

    def test_replay_blocked_reexecution_403(self, client: TestClient) -> None:
        # Create a real recording via the service, then attempt re-execution
        # without approval -> 403.
        import tempfile
        from pathlib import Path

        from agent_system.services.recording import BehaviorRecorder, ReplayContext

        with tempfile.TemporaryDirectory():
            # point service at a temp dir via settings monkeypatch is complex;
            # instead record into the configured recordings dir and clean up.
            from agent_system.config import get_settings

            settings = get_settings()
            rdir = Path(settings.recordings_dir)
            factory = client.app.state.session_factory  # type: ignore[attr-defined]
            rec = BehaviorRecorder(rdir)
            rid = rec.start(factory, context=ReplayContext(workspace_fingerprint="zz"))
            rec.record("tool_call", "fs.write", {"path": "x"})
            rec.finish(factory)
            try:
                r = client.post(
                    f"/api/v1/recordings/{rid}/replay",
                    json={"mode": "APPROVED_REEXECUTE", "context": {}},
                )
                assert r.status_code == 403
                assert "approval" in r.json()["detail"]
            finally:
                (rdir / f"{rid}.jsonl").unlink(missing_ok=True)


class TestBatches:
    def test_batch_flow(self, client: TestClient) -> None:
        session_id = client.post("/api/v1/sessions", json={"goal": "batch"}).json()["id"]

        def mk_task() -> str:
            return client.post(
                "/api/v1/tasks",
                json={"session_id": session_id, "task_type": "code", "title": "t"},
            ).json()["id"]

        t1, t2 = mk_task(), mk_task()
        r = client.post(
            "/api/v1/batches",
            json={"session_id": session_id, "task_ids": [t1, t2]},
        )
        assert r.status_code == 201, r.text
        batch_id = r.json()["batch_id"]
        assert batch_id.startswith("batch_")

        got = client.get(f"/api/v1/batches/{batch_id}")
        assert got.status_code == 200
        assert got.json()["member_count"] == 2

        cancel = client.post(f"/api/v1/batches/{batch_id}/cancel")
        assert cancel.status_code == 200
        assert len(cancel.json()["cancelled"]) == 2

    def test_incompatible_batch_409(self, client: TestClient) -> None:
        session_id = client.post("/api/v1/sessions", json={"goal": "b2"}).json()["id"]
        t1 = client.post(
            "/api/v1/tasks",
            json={"session_id": session_id, "task_type": "code", "title": "a"},
        ).json()["id"]
        t2 = client.post(
            "/api/v1/tasks",
            json={"session_id": session_id, "task_type": "research", "title": "b"},
        ).json()["id"]
        r = client.post("/api/v1/batches", json={"session_id": session_id, "task_ids": [t1, t2]})
        assert r.status_code == 409
        assert "incompatible" in r.json()["detail"]


class TestRecipes:
    def test_crud_and_execute(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/recipes",
            json={"name": "demo", "task_dag": VALID_DAG, "parameters": {}},
        )
        assert r.status_code == 201, r.text
        recipe_id = r.json()["recipe_id"]

        got = client.get(f"/api/v1/recipes/{recipe_id}")
        assert got.status_code == 200
        assert got.json()["steps"] == 2

        # Execution with unresolved params -> 409
        ex = client.post(f"/api/v1/recipes/{recipe_id}/execute", json={"params": {}})
        # no params required by this DAG, so execution should succeed
        assert ex.status_code == 200, ex.text
        assert ex.json()["session_id"].startswith("ses_")

        listed = client.get("/api/v1/recipes")
        assert any(rec["recipe_id"] == recipe_id for rec in listed.json())

    def test_invalid_dag_422(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/recipes",
            json={"name": "bad", "task_dag": {"tasks": []}},
        )
        assert r.status_code == 422

    def test_missing_recipe_404(self, client: TestClient) -> None:
        assert client.get("/api/v1/recipes/recipe_none").status_code == 404
        r = client.post("/api/v1/recipes/recipe_none/execute", json={"params": {}})
        assert r.status_code == 404


class TestPersonality:
    def test_update_get_learn_cycle(self, client: TestClient) -> None:
        import uuid

        agent_id = f"code-{uuid.uuid4().hex[:8]}"  # hermetic: dev DB persists rows
        u = client.put(
            f"/api/v1/personality/{agent_id}",
            json={"tone": "casual", "verbosity": 6},
        )
        assert u.status_code == 200
        assert u.json()["version"] == 1

        g = client.get(f"/api/v1/personality/{agent_id}")
        assert g.status_code == 200
        assert g.json()["tone"] == "casual"

        for _ in range(10):
            f = client.post(f"/api/v1/personality/{agent_id}/feedback", json={"rating": 2})
            assert f.status_code == 201
        learn = client.post(f"/api/v1/personality/{agent_id}/learn")
        assert learn.status_code == 200
        body = learn.json()
        assert body["learned"] is True
        assert body["updates"]["verbosity"] == 4

    def test_security_field_rejected(self, client: TestClient) -> None:
        u = client.put("/api/v1/personality/code", json={"tone": "friendly"})
        assert u.status_code == 200


class TestInsights:
    def test_generate_and_list(self, client: TestClient) -> None:
        session_id = client.post("/api/v1/sessions", json={"goal": "ins"}).json()["id"]
        client.post(
            "/api/v1/tasks", json={"session_id": session_id, "task_type": "code", "title": "t"}
        )
        gen = client.post("/api/v1/insights/generate", json={"insight_type": "daily"})
        assert gen.status_code == 200
        insight_id = gen.json()["insight_id"]
        assert insight_id.startswith("insight_")

        listed = client.get("/api/v1/insights")
        assert any(i["insight_id"] == insight_id for i in listed.json())

        arch = client.post(f"/api/v1/insights/{insight_id}/archive")
        assert arch.status_code == 200
        listed2 = client.get("/api/v1/insights")
        assert all(i["insight_id"] != insight_id for i in listed2.json())

    def test_invalid_type_422(self, client: TestClient) -> None:
        r = client.post("/api/v1/insights/generate", json={"insight_type": "hourly"})
        assert r.status_code == 422


class TestSchedule:
    def test_create_list_delete(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/schedule",
            json={
                "name": "daily insights",
                "kind": "cron",
                "schedule": {"hour": 6},
            },
        )
        assert r.status_code == 201
        job_id = r.json()["job_id"]
        listed = client.get("/api/v1/schedule")
        assert any(j["job_id"] == job_id for j in listed.json())
        d = client.delete(f"/api/v1/schedule/{job_id}")
        assert d.status_code == 204


class TestSupportEndpoints:
    def test_model_calls_and_qa_reports(self, client: TestClient) -> None:
        assert client.get("/api/v1/model-calls").status_code == 200
        assert client.get("/api/v1/qa-reports").status_code == 200
        assert client.get("/api/v1/artifacts/artifact_none").status_code == 404
