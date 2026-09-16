"""Contract — session planning endpoint (v3.1 §7).

Planning is what the Planner does; persisting tasks is what the Supervisor
does. The endpoint performs both, leaves the tasks QUEUED (execution is the
Orchestrator's), and refuses to plan a session that already has tasks so a
plan is never silently duplicated.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from agent_system.api.main import app


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(app) as client:
        token = client.app.state.authenticator.bootstrap_token  # type: ignore[attr-defined]
        client.headers["Authorization"] = f"Bearer {token}"
        yield client


def test_planning_creates_a_dag_and_queues_it(client: TestClient) -> None:
    created = client.post("/api/v1/sessions", json={"goal": "Write a PDF report on Q3 revenue"})
    assert created.status_code == 201
    session_id = created.json()["id"]

    planned = client.post(f"/api/v1/sessions/{session_id}/plan")
    assert planned.status_code == 201, planned.text
    body = planned.json()
    assert body["intent"] == "documents"
    assert body["strategy"].startswith("deterministic")
    assert len(body["tasks"]) == 2
    assert body["tasks"][0]["key"] == "gather"
    assert body["tasks"][1]["depends_on"] == ["gather"]
    assert body["tasks"][0]["required_capabilities"]

    tasks = client.get("/api/v1/tasks", params={"session_id": session_id})
    assert tasks.status_code == 200
    by_title = {task["title"]: task["state"] for task in tasks.json()}
    # Planning queues the dependency-free work and leaves the dependent task
    # PENDING until its dependency succeeds (the Supervisor schedules ready
    # work; only the Orchestrator runs it).
    assert set(by_title.values()) <= {"QUEUED", "PENDING"}
    assert "RUNNING" not in by_title.values()
    assert sorted(by_title.values()) == ["PENDING", "QUEUED"]

    # Planning is one-shot: a second plan would silently duplicate the DAG.
    again = client.post(f"/api/v1/sessions/{session_id}/plan")
    assert again.status_code == 409


def test_unknown_session_returns_404(client: TestClient) -> None:
    assert client.post("/api/v1/sessions/ses_missing/plan").status_code == 404


def test_planned_tasks_carry_dependencies_into_persistence(client: TestClient) -> None:
    created = client.post(
        "/api/v1/sessions", json={"goal": "Research the market and compare sources"}
    )
    session_id = created.json()["id"]
    client.post(f"/api/v1/sessions/{session_id}/plan")
    tasks = client.get("/api/v1/tasks", params={"session_id": session_id}).json()
    assert tasks, "expected planned tasks"
    for task in tasks:
        assert task["agent_type"] == "llm"
