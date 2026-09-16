"""Unit tests — chat REPL (routing, goal flow, slash commands, auth)."""

from __future__ import annotations

from typing import Any

import pytest

from agent_system.cli import chat as chat_mod
from agent_system.cli.main import ApiError


@pytest.fixture()
def api(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub chat's HTTP layer with a scripted responder."""
    calls: list[dict[str, Any]] = []
    responses: dict[tuple[str, str], Any] = {}

    def fake(method: str, path: str, **kwargs: Any) -> Any:
        calls.append({"method": method, "path": path, "kwargs": kwargs})
        key = (method, path)
        if key in responses:
            result = responses[key]
            if isinstance(result, Exception):
                raise result
            return result() if callable(result) else result
        raise AssertionError(f"unexpected API call {method} {path}")

    monkeypatch.setattr(chat_mod, "api_request", fake)
    monkeypatch.setattr(chat_mod, "POLL_INTERVAL_S", 0)
    return {"calls": calls, "responses": responses}


@pytest.fixture()
def authed(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_system.cli.main import _state

    monkeypatch.setitem(_state, "token", "tok")


class TestHandleLine:
    def test_blank_ignored(self, api: dict[str, Any]) -> None:
        state = chat_mod.ChatState()
        chat_mod.handle_line(state, "   ")
        assert api["calls"] == []

    def test_unknown_command_hint(self, api: dict[str, Any], capsys) -> None:
        state = chat_mod.ChatState()
        chat_mod.handle_line(state, "/frobnicate x")
        assert "unknown command" in capsys.readouterr().out
        assert api["calls"] == []

    def test_exit_stops(self, api: dict[str, Any]) -> None:
        state = chat_mod.ChatState()
        chat_mod.handle_line(state, "/exit")
        assert state.running is False

    def test_plain_text_sends_goal(self, api: dict[str, Any]) -> None:
        api["responses"][("POST", "/api/v1/sessions")] = {
            "id": "ses_abc123",
            "goal": "hi",
            "status": "ACTIVE",
        }
        api["responses"][("GET", "/api/v1/events")] = []
        api["responses"][("GET", "/api/v1/tasks")] = []
        state = chat_mod.ChatState()
        chat_mod.handle_line(state, "hello bob")
        assert state.session_id == "ses_abc123"
        assert ("POST", "/api/v1/sessions") in [(c["method"], c["path"]) for c in api["calls"]]


class TestTail:
    def test_tail_renders_events_then_summary(self, api: dict[str, Any], capsys) -> None:
        api["responses"][("GET", "/api/v1/events")] = [
            {
                "sequence": 1,
                "type": "task.created",
                "actor": "user",
                "payload": {"title": "Do thing"},
            },
            {
                "sequence": 2,
                "type": "task.completed",
                "actor": "qa",
                "payload": {"title": "Do thing"},
            },
        ]
        api["responses"][("GET", "/api/v1/tasks")] = [
            {"id": "t1", "title": "Do thing", "state": "COMPLETED"}
        ]
        state = chat_mod.ChatState(session_id="ses_x", cursor=0)
        chat_mod.tail_session(state, timeout=5)
        out = capsys.readouterr().out
        assert "task.created" in out
        assert "1 completed, 0 failed" in out
        assert state.cursor == 2

    def test_tail_stops_when_quiet(self, api: dict[str, Any]) -> None:
        api["responses"][("GET", "/api/v1/events")] = []
        api["responses"][("GET", "/api/v1/tasks")] = []
        state = chat_mod.ChatState(session_id="ses_x")
        chat_mod.tail_session(state, timeout=5)
        polls = [c for c in api["calls"] if c["path"] == "/api/v1/events"]
        assert len(polls) == chat_mod.QUIET_POLLS_TO_STOP


class TestSlashCommands:
    def test_attach_prefix_match(self, api: dict[str, Any]) -> None:
        api["responses"][("GET", "/api/v1/sessions")] = [
            {"id": "ses_aaa111", "goal": "first", "status": "ACTIVE"},
            {"id": "ses_bbb222", "goal": "second", "status": "ACTIVE"},
        ]
        state = chat_mod.ChatState()
        chat_mod.handle_line(state, "/attach ses_bbb")
        assert state.session_id == "ses_bbb222"

    def test_attach_no_match(self, api: dict[str, Any], capsys) -> None:
        api["responses"][("GET", "/api/v1/sessions")] = []
        state = chat_mod.ChatState()
        chat_mod.handle_line(state, "/attach zzz")
        assert "no session" in capsys.readouterr().out
        assert state.session_id is None

    def test_approve_and_deny(self, api: dict[str, Any]) -> None:
        api["responses"][("POST", "/api/v1/approvals/a1/decision")] = {}
        state = chat_mod.ChatState()
        chat_mod.handle_line(state, "/approve a1")
        assert api["calls"][-1]["kwargs"] == {"json": {"approve": True, "policy": "ALLOW_ONCE"}}
        chat_mod.handle_line(state, "/approve a1 --deny")
        assert api["calls"][-1]["kwargs"] == {"json": {"approve": False, "policy": "ALLOW_ONCE"}}

    def test_skills_toggle(self, api: dict[str, Any]) -> None:
        api["responses"][("PATCH", "/api/v1/skills/demo")] = {"name": "demo"}
        state = chat_mod.ChatState()
        chat_mod.handle_line(state, "/skills off demo")
        assert api["calls"][-1]["kwargs"] == {"json": {"enabled": False}}

    def test_api_error_does_not_kill_repl(self, api: dict[str, Any], capsys) -> None:
        api["responses"][("GET", "/api/v1/sessions")] = ApiError(6, "down")
        state = chat_mod.ChatState()
        chat_mod.handle_line(state, "/sessions")
        assert "down" in capsys.readouterr().out
        assert state.running is True


class TestEnsureToken:
    def test_existing_state_token(self, authed: None) -> None:
        assert chat_mod.ensure_token() is True

    def test_env_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agent_system.cli.main import _state

        monkeypatch.setitem(_state, "token", None)
        monkeypatch.setenv("AGENTCTL_TOKEN", "env-tok")
        assert chat_mod.ensure_token() is True
        assert _state["token"] == "env-tok"

    def test_mint_via_secret(self, api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
        from agent_system.cli.main import _state

        monkeypatch.setitem(_state, "token", None)
        monkeypatch.delenv("AGENTCTL_TOKEN", raising=False)
        monkeypatch.setenv("API_SESSION_SECRET", "s3cret")
        api["responses"][("POST", "/api/v1/auth/token")] = {"token": "minted"}
        assert chat_mod.ensure_token() is True
        assert _state["token"] == "minted"

    def test_no_secret_no_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agent_system.cli.main import _state

        monkeypatch.setitem(_state, "token", None)
        monkeypatch.delenv("AGENTCTL_TOKEN", raising=False)
        monkeypatch.delenv("API_SESSION_SECRET", raising=False)
        monkeypatch.setattr(chat_mod, "api_request", lambda *a, **k: None)
        # _defaults reads real .env files; force empty to isolate the path.
        monkeypatch.setattr("agent_system.cli.setup._defaults", lambda: {})
        assert chat_mod.ensure_token() is False


class TestRunRepl:
    def test_repl_runs_until_exit(
        self, api: dict[str, Any], authed: None, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        lines = iter(["/status", "/exit"])
        monkeypatch.setattr(chat_mod.console, "input", lambda prompt="": next(lines))
        monkeypatch.setattr(chat_mod, "_enable_history", lambda: None)
        api["responses"][("GET", "/api/v1/health")] = {"status": "ok"}
        api["responses"][("GET", "/api/v1/ready")] = {"status": "ok", "checks": {}}
        assert chat_mod.run_repl() == 0
        out = capsys.readouterr().out
        assert "Bob Agent" in out

    def test_repl_no_auth_returns_3(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(chat_mod, "ensure_token", lambda: False)
        assert chat_mod.run_repl() == 3
