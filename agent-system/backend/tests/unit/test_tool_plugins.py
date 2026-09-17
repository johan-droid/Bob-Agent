"""P2 — Pluggable tool registration.

Acceptance: a read-only sample-style plugin loads and is callable from the
ReAct loop; an execute-risk plugin is forced through DockerSandbox (never
in-process); disabling removes it from prompt_block() immediately.
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_system.services.agent_loop import run_tool_loop
from agent_system.services.tool_plugins import ToolPluginError, ToolPluginManager
from agent_system.services.tools import ToolContext, build_registry

READ_PLUGIN_META = {
    "description": "Count words (read-only sample).",
    "parameters": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
    "risk": "read",
    "handler": "handler.py:handle",
}

READ_HANDLER = """\
def handle(args: dict) -> dict:
    return {"words": len(str(args.get("text") or "").split())}
"""

EXEC_HANDLER = """\
SPY = []

def handle(args: dict) -> dict:
    SPY.append(args)
    return {"ran": "in-process — must never happen for execute risk"}
"""


def _write_plugin(root: Path, name: str, meta: dict[str, Any], handler_src: str) -> Path:
    plug = root / name
    plug.mkdir(parents=True)
    (plug / "tool.json").write_text(_json.dumps(meta), encoding="utf-8")
    (plug / "handler.py").write_text(handler_src, encoding="utf-8")
    return plug


def _settings(plugin_dir: Path, **overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "tools_plugin_dir": str(plugin_dir),
        "tools_require_approval": False,
        "tools_shell_mode": "sandbox",
        "max_execution_time_seconds": 60,
        "openconnector_base_url": "",
        "mcp_servers": "[]",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestReadOnlyPlugin:
    def test_loads_and_callable_from_react_loop(self, tmp_path: Path) -> None:
        plugdir = tmp_path / "plugins"
        _write_plugin(plugdir, "wordcount", READ_PLUGIN_META, READ_HANDLER)
        registry = build_registry(_settings(plugdir))
        assert "wordcount" in registry.names()
        assert "wordcount" in registry.prompt_block()

        seen = {"n": 0}

        def invoke(_transcript: str) -> dict[str, Any]:
            seen["n"] += 1
            if seen["n"] == 1:
                return {"output": '```tool:wordcount\n{"text": "hello brave world"}\n```'}
            return {"output": "3 words"}

        ctx = ToolContext(settings=_settings(plugdir))
        result = run_tool_loop(
            invoke=invoke,
            system="sys",
            task="count words",
            registry=registry,
            ctx=ctx,
            max_iters=3,
        )
        assert result.stopped == "done"
        assert result.tool_calls == 1

    def test_handler_result_returned(self, tmp_path: Path) -> None:
        plugdir = tmp_path / "plugins"
        _write_plugin(plugdir, "wordcount", READ_PLUGIN_META, READ_HANDLER)
        registry = build_registry(_settings(plugdir))
        tool = registry.get("wordcount")
        assert tool is not None
        out = tool.handler({"text": "a b c"}, ToolContext(settings=_settings(plugdir)))
        assert out == {"words": 3}


class TestExecuteRiskSandboxing:
    def test_execute_plugin_forced_through_docker_sandbox(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plugdir = tmp_path / "plugins"
        meta = dict(READ_PLUGIN_META, risk="execute", description="dangerous sample")
        _write_plugin(plugdir, "doer", meta, EXEC_HANDLER)
        sandbox_calls: list[dict[str, Any]] = []

        class FakeSandbox:
            def __init__(self, *a: Any, **k: Any) -> None:
                pass

            def run(
                self, workspace: str, command: str, timeout_seconds: Any = None
            ) -> dict[str, Any]:
                sandbox_calls.append({"workspace": workspace, "command": command})
                assert "python3 /ws/_runner.py" in command
                assert (Path(workspace) / "handler.py").exists()
                assert (Path(workspace) / "args.json").exists()
                return {"exit_code": 0, "stdout": '{"ran": "sandbox"}\n'}

        # Patch the lazy import site inside _run_in_sandbox.
        import agent_system.services.sandbox as _sb

        monkeypatch.setattr(_sb, "DockerSandbox", FakeSandbox)

        registry = build_registry(_settings(plugdir))
        tool = registry.get("doer")
        assert tool is not None
        assert tool.risk == "execute"
        out = tool.handler({"x": 1}, ToolContext(settings=_settings(plugdir)))
        assert out == {"ran": "sandbox"}
        assert len(sandbox_calls) == 1  # forced through DockerSandbox

    def test_execute_plugin_demands_approval_like_shell(self, tmp_path: Path) -> None:
        from agent_system.services.tools import NeedsApprovalError
        from agent_system.services.tools.execution import execute_tool

        plugdir = tmp_path / "plugins"
        meta = dict(READ_PLUGIN_META, risk="execute", description="dangerous sample")
        _write_plugin(plugdir, "doer", meta, EXEC_HANDLER)
        settings = _settings(plugdir, tools_require_approval=True)
        registry = build_registry(settings)
        tool = registry.get("doer")
        assert tool is not None
        with pytest.raises(NeedsApprovalError):
            execute_tool(tool, {"text": "test"}, ToolContext(settings=settings, factory=None))


class TestEnableDisable:
    def test_disabling_removes_from_prompt_block(self, tmp_path: Path) -> None:
        plugdir = tmp_path / "plugins"
        _write_plugin(plugdir, "wordcount", READ_PLUGIN_META, READ_HANDLER)
        manager = ToolPluginManager(plugdir)
        assert "wordcount" in build_registry(_settings(plugdir)).names()
        manager.set_enabled("wordcount", False)
        registry = build_registry(_settings(plugdir))
        assert "wordcount" not in registry.names()
        assert "wordcount" not in registry.prompt_block()
        manager.set_enabled("wordcount", True)
        assert "wordcount" in build_registry(_settings(plugdir)).names()

    def test_unknown_plugin_toggle_rejected(self, tmp_path: Path) -> None:
        manager = ToolPluginManager(tmp_path / "plugins")
        with pytest.raises(ToolPluginError, match="not found"):
            manager.set_enabled("nope", False)


class TestValidation:
    def test_bad_risk_rejected(self, tmp_path: Path) -> None:
        plugdir = tmp_path / "plugins"
        meta = dict(READ_PLUGIN_META, risk="godmode")
        _write_plugin(plugdir, "evil", meta, READ_HANDLER)
        manager = ToolPluginManager(plugdir)
        assert manager.discover() == []
        assert any("godmode" in e for e in manager.last_errors)
        assert "evil" not in build_registry(_settings(plugdir)).names()

    def test_builtin_shadow_refused(self, tmp_path: Path) -> None:
        plugdir = tmp_path / "plugins"
        _write_plugin(plugdir, "shell", READ_PLUGIN_META, READ_HANDLER)
        registry = build_registry(_settings(plugdir))
        assert registry.get("shell") is not None
        # Built-in shell handler survives (plugin did not shadow it).
        assert registry.get("shell").risk == "execute"

    def test_missing_dir_means_no_plugins(self, tmp_path: Path) -> None:
        registry = build_registry(_settings(tmp_path / "does-not-exist"))
        assert "shell" in registry.names()
