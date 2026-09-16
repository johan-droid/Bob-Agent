"""Unit tests — agentctl skills commands (stubbed API layer)."""

from __future__ import annotations

from typing import Any

from typer.testing import CliRunner

from agent_system.cli.main import EXIT_OK, app

runner = CliRunner()


def _stub(monkeypatch, handler) -> dict[str, Any]:
    calls: dict[str, Any] = {}

    def fake(method: str, path: str, **kwargs: Any) -> Any:
        calls["method"] = method
        calls["path"] = path
        calls["kwargs"] = kwargs
        return handler(method, path, kwargs)

    from agent_system.cli import main as main_mod

    monkeypatch.setattr(main_mod, "api_request", fake)
    return calls


class TestList:
    def test_list_hits_api(self, monkeypatch) -> None:
        calls = _stub(
            monkeypatch,
            lambda m, p, k: {"skills": [{"name": "a", "enabled": True}], "errors": []},
        )
        result = runner.invoke(app, ["skills", "list"])
        assert result.exit_code == EXIT_OK, result.output
        assert calls["path"] == "/api/v1/skills"
        assert "a" in result.output

    def test_list_json(self, monkeypatch) -> None:
        import json

        _stub(monkeypatch, lambda m, p, k: {"skills": [], "errors": []})
        result = runner.invoke(app, ["--json", "skills", "list"])
        assert result.exit_code == EXIT_OK
        assert json.loads(result.output)["skills"] == []


class TestNew:
    def test_new_posts_contract(self, monkeypatch) -> None:
        calls = _stub(monkeypatch, lambda m, p, k: {"name": "x", "version": "0.1.0"})

        result = runner.invoke(
            app,
            ["skills", "new", "x", "--description", "Does x", "--instructions", "Do x."],
        )
        assert result.exit_code == EXIT_OK, result.output
        assert calls == {
            "method": "POST",
            "path": "/api/v1/skills",
            "kwargs": {
                "json": {
                    "name": "x",
                    "description": "Does x",
                    "instructions": "Do x.",
                    "agents": [],
                    "config": {},
                    "author": "user",
                }
            },
        }

    def test_new_requires_instructions(self, monkeypatch) -> None:
        _stub(monkeypatch, lambda m, p, k: {})
        result = runner.invoke(app, ["skills", "new", "x", "--description", "d"])
        assert result.exit_code != EXIT_OK


class TestMutations:
    def test_enable_disable(self, monkeypatch) -> None:
        calls = _stub(monkeypatch, lambda m, p, k: {"name": "x"})
        assert runner.invoke(app, ["skills", "enable", "x"]).exit_code == EXIT_OK
        assert calls["kwargs"] == {"json": {"enabled": True}}
        assert runner.invoke(app, ["skills", "disable", "x"]).exit_code == EXIT_OK
        assert calls["kwargs"] == {"json": {"enabled": False}}

    def test_config_set(self, monkeypatch) -> None:
        calls = _stub(monkeypatch, lambda m, p, k: {"config": {"depth": 3}})
        result = runner.invoke(app, ["skills", "config", "x", "--set", "depth=3"])
        assert result.exit_code == EXIT_OK, result.output
        assert calls["kwargs"] == {"json": {"config": {"depth": 3}}}

    def test_rm(self, monkeypatch) -> None:
        calls = _stub(monkeypatch, lambda m, p, k: None)
        result = runner.invoke(app, ["skills", "rm", "x"])
        assert result.exit_code == EXIT_OK
        assert calls["method"] == "DELETE"
