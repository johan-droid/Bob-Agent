"""Contract tests — agentctl CLI (v3.1 Phase 10 acceptance).

CLI rule (§34): every command goes through /api/v1 — a stub HTTP backend
verifies exact paths/payloads, JSON schema stability, and exit codes.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from agent_system.cli.main import (
    EXIT_AUTH,
    EXIT_CONFLICT,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_UNAVAILABLE,
    app,
)
from agent_system.cli_stub_server import StubBackend

runner = CliRunner()


@pytest.fixture()
def backend(monkeypatch: pytest.MonkeyPatch) -> StubBackend:
    """Run the CLI against a stub /api/v1 backend over real HTTP."""
    stub = StubBackend()
    monkeypatch.setenv("AGENTCTL_BASE_URL", stub.base_url)
    # Route all CLI calls at the stub via the --api-url mechanism.
    from agent_system.cli import main as cli_mod

    orig_client = cli_mod.client

    def stub_client() -> Any:
        import httpx

        return httpx.Client(base_url=stub.base_url, timeout=10.0)

    monkeypatch.setattr(cli_mod, "client", stub_client)
    yield stub
    orig_client  # noqa: B018 — keeps reference alive for clarity
    stub.close()


def test_version_json_schema(backend: StubBackend) -> None:
    result = runner.invoke(app, ["--json", "version"])
    assert result.exit_code == EXIT_OK
    payload = json.loads(result.stdout)
    assert payload["tool"] == "agentctl"
    assert payload["api"] == "v1"
    assert set(payload.keys()) == {"tool", "version", "api"}


def test_status_hits_health_and_ready(backend: StubBackend) -> None:
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == EXIT_OK
    payload = json.loads(result.stdout)
    assert payload["health"]["status"] == "ok"
    assert "checks" in payload["ready"]
    assert backend.requests[0]["path"] == "/api/v1/health"
    assert backend.requests[1]["path"] == "/api/v1/ready"


def test_session_create_via_api(backend: StubBackend) -> None:
    result = runner.invoke(app, ["--json", "sessions", "create", "Build a FastAPI scaffold"])
    assert result.exit_code == EXIT_OK
    payload = json.loads(result.stdout)
    assert payload["id"].startswith("ses_")
    req = backend.requests[0]
    assert req["method"] == "POST"
    assert req["path"] == "/api/v1/sessions"
    assert req["json"]["goal"] == "Build a FastAPI scaffold"


def test_tasks_list_forwards_filters(backend: StubBackend) -> None:
    result = runner.invoke(
        app, ["--json", "tasks", "list", "--session", "ses_1", "--state", "RUNNING"]
    )
    assert result.exit_code == EXIT_OK
    req = backend.requests[0]
    assert req["path"] == "/api/v1/tasks"
    assert req["params"]["session_id"] == "ses_1"
    assert req["params"]["state"] == "RUNNING"


def test_tasks_retry_uses_api(backend: StubBackend) -> None:
    result = runner.invoke(app, ["--json", "tasks", "retry", "task_01"])
    assert result.exit_code == EXIT_OK
    req = backend.requests[0]
    assert req["method"] == "POST"
    assert req["path"] == "/api/v1/tasks/task_01/retry"


def test_approvals_decide_payload(backend: StubBackend) -> None:
    result = runner.invoke(
        app, ["--json", "approvals", "decide", "approval_01", "--deny", "--policy", "ALLOW_ONCE"]
    )
    assert result.exit_code == EXIT_OK
    req = backend.requests[0]
    assert req["path"] == "/api/v1/approvals/approval_01/decision"
    assert req["json"]["approve"] is False


def test_404_maps_to_exit_code_4(backend: StubBackend) -> None:
    backend.next_status = 404
    result = runner.invoke(app, ["--json", "tasks", "get", "task_missing"])
    assert result.exit_code == EXIT_NOT_FOUND


def test_401_maps_to_exit_code_3(backend: StubBackend) -> None:
    backend.next_status = 401
    result = runner.invoke(app, ["--json", "tasks", "list"])
    assert result.exit_code == EXIT_AUTH


def test_409_maps_to_exit_code_5(backend: StubBackend) -> None:
    backend.next_status = 409
    result = runner.invoke(app, ["--json", "tasks", "retry", "task_01"])
    assert result.exit_code == EXIT_CONFLICT


def test_503_maps_to_exit_code_6(backend: StubBackend) -> None:
    backend.next_status = 503
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == EXIT_UNAVAILABLE


def test_connection_refused_maps_to_exit_code_6(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_system.cli import main as cli_mod

    def refused_client() -> Any:
        import httpx

        return httpx.Client(base_url="http://127.0.0.1:1", timeout=2.0)

    monkeypatch.setattr(cli_mod, "client", refused_client)
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == EXIT_UNAVAILABLE
