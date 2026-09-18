"""Integration — the vault MCP server enters the canonical execution path.

The new capability must not be a bypass. This suite drives Bob's *real* MCP
client (``services/mcp.py`` through the ``mcp_list`` / ``mcp_call`` capabilities
in ``services/tools/optional.py``) against the *real* shipped server
(``agent_system.mcp_servers.vault``, spawned as a stdio subprocess), and proves:

1. discovery happens through the registry capability, not a private path;
2. arguments are validated by the canonical validator;
3. the permission gate demands a live per-call approval on
   ``mcp:vault:<tool>`` **before** anything is written to the vault;
4. an approved call actually updates the Bob Agent record;
5. an unavailable/broken server is a clean ``ToolError`` — it can never crash
   Bob globally.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_system.infra.db import make_engine, make_session_factory
from agent_system.infra.models import Base
from agent_system.services.permissions import PermissionGate, Policy
from agent_system.services.tool_errors import NeedsApprovalError, ToolError
from agent_system.services.tools import ToolContext, build_registry, validate_arguments
from agent_system.services.tools.execution import execute_tool


@pytest.fixture()
def factory(tmp_path: Path) -> Any:
    engine = make_engine(f"sqlite:///{tmp_path / 'vault-mcp.db'}")
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


def _server_json(
    vault_root: Path, command: str | None = None, args: list[str] | None = None
) -> str:
    """The documented MCP_SERVERS entry, pointed at the shipped server."""
    entry: dict[str, Any] = {
        "name": "vault",
        "command": command or sys.executable,
        "args": args
        if args is not None
        else ["-m", "agent_system.mcp_servers.vault", "--vault", str(vault_root)],
        "timeout": 60,
    }
    return json.dumps([entry])


def _settings(tmp_path: Path, vault_root: Path, **overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "tools_shell_mode": "off",
        "tools_require_approval": True,
        "tools_fs_roots": str(tmp_path),
        "tools_plugin_dir": str(tmp_path / "plugins"),
        "openconnector_base_url": "",
        "mcp_servers": _server_json(vault_root),
        "max_file_size_mb": 1,
        "max_execution_time_seconds": 30,
        "agent_env": "test",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _ctx(factory: Any, settings: Any) -> ToolContext:
    return ToolContext(
        settings=settings,
        factory=factory,
        session_id="ses_vault",
        task_id="task_vault",
        agent_run_id="run_vault",
        agent_type="generic",
        gate=PermissionGate(factory=factory),
    )


class TestCanonicalDiscovery:
    def test_registry_exposes_mcp_capabilities_when_the_server_is_configured(
        self, factory: Any, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path, tmp_path / "vault")
        registry = build_registry(settings)
        assert registry.get("mcp_list") is not None
        tool = registry.get("mcp_call")
        assert tool is not None
        # Canonical validation accepts the documented shape... and rejects a bad one.
        assert (
            validate_arguments(
                "mcp_call",
                tool.parameters,
                {"server": "vault", "tool": "vault_record", "arguments": {}},
            )
            == []
        )
        assert validate_arguments("mcp_call", tool.parameters, {"server": "vault"}) != []
        # ...and the capability is execute-tier (approval-gated), not a read.
        assert tool.tier.value == "execute"

    def test_vault_server_and_its_tools_are_discoverable(
        self, factory: Any, tmp_path: Path
    ) -> None:
        from agent_system.services.tools.optional import _mcp_list

        ctx = _ctx(factory, _settings(tmp_path, tmp_path / "vault"))
        servers = _mcp_list({}, ctx)
        assert "vault" in servers["servers"]
        listing = _mcp_list({"server": "vault"}, ctx)
        names = {row["name"] for row in listing["tools"]}
        assert {"vault_write_note", "vault_record", "vault_read_record"} <= names
        assert listing["count"] == 6
        assert all(len(row["description"]) <= 160 for row in listing["tools"])


class TestApprovalGateBeforeAnyWrite:
    def test_call_requires_approval_and_touches_nothing(self, factory: Any, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        ctx = _ctx(factory, _settings(tmp_path, vault_root))
        tool = build_registry(ctx.settings).get("mcp_call")
        assert tool is not None
        with pytest.raises(NeedsApprovalError):
            execute_tool(
                tool,
                {
                    "server": "vault",
                    "tool": "vault_record",
                    "arguments": {"event": "unapproved"},
                },
                ctx,
            )
        # The gate blocked the call before the server process was even asked.
        assert not (vault_root / "records" / "bob-agent.md").exists()

    def test_approved_call_updates_the_bob_agent_record(self, factory: Any, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        ctx = _ctx(factory, _settings(tmp_path, vault_root))
        tool = build_registry(ctx.settings).get("mcp_call")
        assert tool is not None
        args = {
            "server": "vault",
            "tool": "vault_record",
            "arguments": {"event": "release-gate", "details": "approved path"},
        }
        with pytest.raises(NeedsApprovalError) as pending:
            execute_tool(tool, args, ctx)
        ctx.gate.decide(pending.value.approval_id, approve=True, policy=Policy.ALLOW_ONCE)

        result = execute_tool(tool, args, ctx)
        assert result["isError"] is False
        record = vault_root / "records" / "bob-agent.md"
        assert record.is_file()
        assert "type: agent-record" in record.read_text(encoding="utf-8")

    def test_scope_is_per_server_and_tool(self) -> None:
        from agent_system.services.tools.optional import _mcp_scope

        assert _mcp_scope({"server": "vault", "tool": "vault_record"}) == "mcp:vault:vault_record"


class TestFailuresStayContained:
    def test_missing_command_is_a_clean_tool_error(self, factory: Any, tmp_path: Path) -> None:
        from agent_system.services.tools.optional import _mcp_call

        settings = _settings(
            tmp_path,
            tmp_path / "vault",
            mcp_servers=_server_json(tmp_path / "vault", command="definitely-not-a-real-binary"),
        )
        with pytest.raises(ToolError, match="mcp call failed"):
            _mcp_call(
                {"server": "vault", "tool": "vault_status", "arguments": {}},
                _ctx(factory, settings),
            )

    def test_server_that_dies_immediately_is_a_clean_tool_error(
        self, factory: Any, tmp_path: Path
    ) -> None:
        from agent_system.services.tools.optional import _mcp_call

        settings = _settings(
            tmp_path,
            tmp_path / "vault",
            mcp_servers=_server_json(tmp_path / "vault", command="true", args=[]),
        )
        with pytest.raises(ToolError):
            _mcp_call(
                {"server": "vault", "tool": "vault_status", "arguments": {}},
                _ctx(factory, settings),
            )

    def test_unknown_tool_on_the_server_is_reported_not_raised(
        self, factory: Any, tmp_path: Path
    ) -> None:
        from agent_system.services.tools.optional import _mcp_call

        ctx = _ctx(factory, _settings(tmp_path, tmp_path / "vault"))
        result = _mcp_call({"server": "vault", "tool": "vault_nope", "arguments": {}}, ctx)
        assert result["isError"] is True
