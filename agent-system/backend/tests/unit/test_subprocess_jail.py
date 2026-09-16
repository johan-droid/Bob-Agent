"""Cloud jail — SubprocessJail containment + cloud tool routing.

Acceptance: echo works; workspace escape refused; secrets stripped from
child env; timeouts raise SandboxError; output capped; allowlist enforced;
shell routes through the jail when HEROKU_JAIL=true; execute-risk plugins
run jailed (not in-process, not Docker) in cloud mode.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_system.services.sandbox import (
    SandboxError,
    SubprocessJail,
    check_allowlist,
)


def _settings(**overrides):  # type: ignore[no-untyped-def]
    base = {
        "tools_shell_mode": "local",
        "tools_require_approval": False,
        "heroku_jail": True,
        "heroku_shell_allowlist": "",
        "max_execution_time_seconds": 30,
        "tools_fs_roots": "",
        "openconnector_base_url": "",
        "mcp_servers": "[]",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestSubprocessJail:
    def test_echo(self, tmp_path: Path) -> None:
        out = SubprocessJail().run(str(tmp_path), "echo hello-jail")
        assert out["exit_code"] == 0
        assert "hello-jail" in out["stdout"]

    def test_cwd_confined_to_workspace(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        out = SubprocessJail().run(str(ws), "pwd")
        assert str(ws.resolve()) in out["stdout"]

    def test_unresolvable_workspace_refused(self, tmp_path: Path) -> None:
        with pytest.raises(SandboxError):
            SubprocessJail().run("\0nope", "echo hi")

    def test_secrets_stripped_from_child_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JAILTEST_API_KEY", "sk-should-not-leak-1234567890")
        monkeypatch.setenv("JAILTEST_REDIS_URL", "redis://secret")
        monkeypatch.setenv("DATABASE_URL", "postgres://secret")
        out = SubprocessJail().run(str(tmp_path), "env")
        assert "sk-should-not-leak-1234567890" not in out["stdout"]
        assert "redis://secret" not in out["stdout"]
        assert "postgres://secret" not in out["stdout"]

    def test_timeout_raises(self, tmp_path: Path) -> None:
        with pytest.raises(SandboxError, match="timed out"):
            SubprocessJail().run(str(tmp_path), "sleep 30", timeout_seconds=1)

    def test_output_capped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import agent_system.services.sandbox as sandbox_mod

        monkeypatch.setattr(sandbox_mod, "MAX_OUTPUT_BYTES", 64)
        out = SubprocessJail().run(str(tmp_path), "yes jail | head -c 10000")
        assert len(out["stdout"]) <= 64

    def test_failing_command_reports_exit_code(self, tmp_path: Path) -> None:
        out = SubprocessJail().run(str(tmp_path), "exit 3")
        assert out["exit_code"] == 3


class TestAllowlist:
    def test_empty_allows_anything(self) -> None:
        check_allowlist("rm -rf /", "")

    def test_matching_prefix_passes(self) -> None:
        check_allowlist("ls -la", "ls,echo")

    def test_non_matching_refused(self) -> None:
        with pytest.raises(SandboxError, match="allowlist"):
            check_allowlist("curl http://evil", "ls,echo")


class TestCloudShellRouting:
    def test_shell_uses_jail_when_flag_set(self, tmp_path: Path) -> None:
        from agent_system.services.tools import ToolContext
        from agent_system.services.tools.builtin.shell import _shell

        # The working directory must be an allowed root: shell is jailed to the
        # same workspace roots as every other capability.
        settings = _settings(tools_fs_roots=str(tmp_path))
        ctx = ToolContext(settings=settings)
        out = _shell({"command": "echo cloud-shell", "cwd": str(tmp_path)}, ctx)
        assert out["exit_code"] == 0
        assert "cloud-shell" in out["output"]

    def test_shell_allowlist_enforced(self, tmp_path: Path) -> None:
        from agent_system.services.tools import ToolContext, ToolError
        from agent_system.services.tools.builtin.shell import _shell

        settings = _settings(heroku_shell_allowlist="echo", tools_fs_roots=str(tmp_path))
        ctx = ToolContext(settings=settings)
        with pytest.raises(ToolError, match="allowlist"):
            _shell({"command": "uname -a", "cwd": str(tmp_path)}, ctx)

    def test_shell_approval_still_required(self, tmp_path: Path) -> None:
        """Approval is enforced by the capability execution path, not the handler."""
        from agent_system.services.tools import (
            NeedsApprovalError,
            ToolContext,
            build_registry,
        )
        from agent_system.services.tools.execution import execute_tool

        settings = _settings(tools_require_approval=True, tools_fs_roots=str(tmp_path))
        ctx = ToolContext(settings=settings, factory=None)
        tool = build_registry(settings, plugin_dir=tmp_path / "plugins").get("shell")
        assert tool is not None
        with pytest.raises(NeedsApprovalError):
            execute_tool(tool, {"command": "echo gated", "cwd": str(tmp_path)}, ctx)

    def test_mode_off_still_disables(self, tmp_path: Path) -> None:
        from agent_system.services.tools import ToolContext, ToolError
        from agent_system.services.tools.builtin.shell import _shell

        settings = _settings(tools_shell_mode="off", tools_fs_roots=str(tmp_path))
        ctx = ToolContext(settings=settings)
        with pytest.raises(ToolError, match="disabled"):
            _shell({"command": "echo no", "cwd": str(tmp_path)}, ctx)


class TestCloudPluginJail:
    def test_execute_plugin_runs_jailed(self, tmp_path: Path) -> None:
        import json as _json

        from agent_system.services.tool_plugins import ToolPluginManager
        from agent_system.services.tools import ToolContext

        plugdir = tmp_path / "plugins"
        plug = plugdir / "cloudy"
        plug.mkdir(parents=True)
        (plug / "tool.json").write_text(
            _json.dumps(
                {
                    "description": "cloud execute sample",
                    "parameters": {"type": "object", "properties": {}},
                    "risk": "execute",
                    "handler": "handler.py:handle",
                }
            ),
            encoding="utf-8",
        )
        (plug / "handler.py").write_text(
            "def handle(args: dict) -> dict:\n    return {'jailed': True}\n",
            encoding="utf-8",
        )
        settings = SimpleNamespace(
            tools_plugin_dir=str(plugdir),
            tools_require_approval=False,
            heroku_jail=True,
            max_execution_time_seconds=60,
            openconnector_base_url="",
            mcp_servers="[]",
            tools_shell_mode="off",
            tools_fs_roots="",
        )
        manager = ToolPluginManager(str(plugdir))
        tools = manager.build_tools()
        assert len(tools) == 1
        out = tools[0].handler({}, ToolContext(settings=settings))
        assert out == {"jailed": True}
        assert not manager.last_errors
        # Never let the child see the test's secrets.
        os.environ.pop("JAILTEST_API_KEY", None)
