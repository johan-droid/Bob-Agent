"""Integration — one authoritative permission path (v3.1 §3, §12–§13).

These tests pin the properties the architecture reconciliation established:

1. An approval decided through the gate the API serves is visible to the
   capability that requested it (previously two independent stores: an
   in-memory gate for the API, DB helpers inside the tools).
2. A gate constructed after a "restart" sees the same durable decisions.
3. No capability path can bypass the gate: every write/execute capability in
   the registry refuses to run without a grant, and destructive capabilities
   can never be granted at all.
4. The capability-risk -> risk-level mapping is canonical and total.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_system.domain.events import utcnow
from agent_system.infra.db import make_engine, make_session_factory
from agent_system.infra.models import Base
from agent_system.services.permissions import (
    CAPABILITY_RISK_TO_RISK,
    ApprovalRequest,
    CapabilityRisk,
    Decision,
    Outcome,
    PermissionGate,
    Policy,
    Risk,
    classify_risk,
    is_dangerous_scope,
)
from agent_system.services.tool_errors import NeedsApprovalError
from agent_system.services.tools import (
    ToolContext,
    build_registry,
    validate_arguments,
)
from agent_system.services.tools.execution import execute_tool, plan_permission


@pytest.fixture()
def factory(tmp_path: Path) -> Any:
    engine = make_engine(f"sqlite:///{tmp_path / 'permissions.db'}")
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


def _settings(tmp_path: Path, **overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "tools_shell_mode": "off",
        "tools_require_approval": True,
        "tools_fs_roots": str(tmp_path),
        "tools_plugin_dir": str(tmp_path / "plugins"),
        "openconnector_base_url": "",
        "mcp_servers": "[]",
        "max_file_size_mb": 1,
        "max_execution_time_seconds": 30,
        "agent_env": "test",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _ctx(factory: Any, tmp_path: Path, gate: PermissionGate, **overrides: Any) -> ToolContext:
    return ToolContext(
        settings=_settings(tmp_path, **overrides),
        factory=factory,
        session_id="ses_perm",
        task_id="task_perm",
        agent_run_id="run_perm",
        agent_type="generic",
        gate=gate,
    )


class TestCanonicalRiskMapping:
    def test_every_tier_maps_to_a_level(self) -> None:
        assert CAPABILITY_RISK_TO_RISK == {
            CapabilityRisk.READ: Risk.LOW,
            CapabilityRisk.WRITE: Risk.MEDIUM,
            CapabilityRisk.EXECUTE: Risk.HIGH,
            CapabilityRisk.DESTRUCTIVE: Risk.CRITICAL,
        }

    @pytest.mark.parametrize("scope", sorted(["host:shell", "browser:transact"]))
    def test_dangerous_scope_escalates_even_a_read_tier(self, scope: str) -> None:
        assert classify_risk(CapabilityRisk.READ, scope) is Risk.CRITICAL

    def test_unknown_tier_fails_safe(self) -> None:
        assert classify_risk("nonsense", "file:read") is Risk.HIGH


class TestApiApprovalUnblocksCapability:
    """The regression this whole refactor exists for."""

    def test_grant_is_visible_to_the_capability_through_a_new_gate(
        self, factory: Any, tmp_path: Path
    ) -> None:
        # The tool asks for permission (its gate is the DB-backed one).
        requesting_ctx = _ctx(factory, tmp_path, PermissionGate(factory=factory))
        tool = build_registry(requesting_ctx.settings).get("file_write")
        assert tool is not None

        with pytest.raises(NeedsApprovalError) as pending:
            execute_tool(tool, {"path": str(tmp_path / "a.txt"), "content": "x"}, requesting_ctx)
        approval_id = pending.value.approval_id
        assert approval_id
        assert not pending.value.denied

        # A different gate instance — the one the API serves — decides it.
        api_gate = PermissionGate(factory=factory)
        api_gate.decide(approval_id, approve=True, policy=Policy.ALLOW_ONCE)

        # A third gate instance (i.e. after a restart) executes the capability.
        after_restart = _ctx(factory, tmp_path, PermissionGate(factory=factory))
        result = execute_tool(
            tool, {"path": str(tmp_path / "a.txt"), "content": "x"}, after_restart
        )
        assert result["bytes"] == 1
        assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "x"

    def test_allow_once_is_consumed_across_processes(self, factory: Any, tmp_path: Path) -> None:
        gate = PermissionGate(factory=factory)
        record = gate.request(
            ApprovalRequest(
                requested_action="file_write",
                risk=Risk.MEDIUM,
                scope="file:write",
                requester="generic",
            )
        )
        gate.decide(record.approval_id, approve=True, policy=Policy.ALLOW_ONCE)
        assert gate.check(
            ApprovalRequest(
                requested_action="file_write", risk=Risk.MEDIUM, scope="file:write", requester="g"
            )
        )[0]
        # A fresh gate (another process) must not see a second grant.
        other = PermissionGate(factory=factory)
        allowed, _ = other.check(
            ApprovalRequest(
                requested_action="file_write", risk=Risk.MEDIUM, scope="file:write", requester="g"
            )
        )
        assert not allowed

    def test_decision_survives_restart(self, factory: Any) -> None:
        gate = PermissionGate(factory=factory)
        record = gate.request(
            ApprovalRequest(
                requested_action="git_commit",
                risk=Risk.MEDIUM,
                scope="git:write:repo",
                requester="generic",
                session_id="ses_x",
            )
        )
        gate.decide(record.approval_id, approve=True, policy=Policy.ALLOW_SESSION)
        restarted = PermissionGate(factory=factory)
        stored = restarted.get(record.approval_id)
        assert stored is not None
        assert stored.decision is Decision.APPROVED
        assert stored.policy is Policy.ALLOW_SESSION
        assert restarted.list_pending() == []


class TestNoCapabilityBypassesTheGate:
    """Every mutating capability refuses without a grant."""

    @pytest.mark.parametrize(
        ("tool_name", "args"),
        [
            ("file_write", {"path": "out/x.txt", "content": "hi"}),
            ("file_edit", {"path": "out/x.txt", "old": "a", "new": "b"}),
            ("file_patch", {"path": "out/x.txt", "patch": "@@ -1,1 +1,1 @@\n-a\n+b"}),
            ("memory_remember", {"fact": "something"}),
            ("document_create", {"kind": "txt", "content": {"body": "hi"}}),
        ],
    )
    def test_mutating_capability_requires_a_grant(
        self, factory: Any, tmp_path: Path, tool_name: str, args: dict[str, Any]
    ) -> None:
        ctx = _ctx(factory, tmp_path, PermissionGate(factory=factory))
        tool = build_registry(ctx.settings).get(tool_name)
        assert tool is not None, tool_name
        assert tool.permission_required(ctx.settings), tool_name
        with pytest.raises(NeedsApprovalError):
            execute_tool(tool, args, ctx)

    def test_read_tier_capability_runs_without_a_grant(
        self, factory: Any, tmp_path: Path
    ) -> None:
        """Read-tier tools are never gated — ``task_status`` reads real state
        without any approval record, where a mutating tool would be refused."""
        from agent_system.infra.db import session_scope
        from agent_system.infra.models import Session as SessionRow
        from agent_system.infra.models import Task as TaskRow

        with session_scope(factory) as db:
            db.add(SessionRow(id="ses_perm", goal="g", status="ACTIVE"))
            db.add(
                TaskRow(
                    id="task_perm_read",
                    session_id="ses_perm",
                    task_type="demo",
                    title="readable",
                    state="QUEUED",
                )
            )
        gate = PermissionGate(factory=factory)
        ctx = _ctx(factory, tmp_path, gate)
        tool = build_registry(ctx.settings).get("task_status")
        assert tool is not None
        assert tool.permission_required(ctx.settings) is False
        assert gate.list_pending() == []  # no approval was ever requested
        outcome = execute_tool(tool, {"task_id": "task_perm_read"}, ctx)
        assert outcome["task_id"] == "task_perm_read"
        assert outcome["state"] == "QUEUED"

    def test_destructive_capabilities_are_default_deny(self, factory: Any, tmp_path: Path) -> None:
        ctx = _ctx(factory, tmp_path, PermissionGate(factory=factory))
        registry = build_registry(ctx.settings)
        destructive = [tool for tool in registry.tools() if tool.tier is CapabilityRisk.DESTRUCTIVE]
        assert destructive, "expected declared destructive capabilities (git_*)"
        for tool in destructive:
            plan = plan_permission(tool, {}, ctx.settings)
            assert plan.default_deny is True
            with pytest.raises(NeedsApprovalError) as refused:
                execute_tool(tool, {"repo": str(tmp_path)}, ctx)
            assert refused.value.denied is True

    def test_approvals_cannot_be_disabled_for_destructive(
        self, factory: Any, tmp_path: Path
    ) -> None:
        ctx = _ctx(factory, tmp_path, PermissionGate(factory=factory), tools_require_approval=False)
        tool = build_registry(ctx.settings).get("git_reset_hard")
        assert tool is not None
        with pytest.raises(NeedsApprovalError) as refused:
            execute_tool(tool, {"repo": str(tmp_path)}, ctx)
        assert refused.value.denied is True

    def test_dangerous_scope_is_never_grantable(self, factory: Any) -> None:
        gate = PermissionGate(factory=factory)
        for scope in ("browser:transact", "host:shell", "credential:transmit"):
            assert is_dangerous_scope(scope)
            record = gate.request(
                ApprovalRequest(
                    requested_action="act", risk=Risk.CRITICAL, scope=scope, requester="agent"
                )
            )
            assert record.decision is Decision.DENIED
            decided = gate.decide(record.approval_id, approve=True, policy=Policy.ALLOW_ALWAYS)
            assert decided.decision is not Decision.APPROVED

    def test_disabled_approvals_still_allow_reads(self, factory: Any, tmp_path: Path) -> None:
        ctx = _ctx(factory, tmp_path, PermissionGate(factory=factory), tools_require_approval=False)
        target = tmp_path / "readme.txt"
        target.write_text("hello", encoding="utf-8")
        tool = build_registry(ctx.settings).get("file_read")
        assert tool is not None
        assert execute_tool(tool, {"path": str(target)}, ctx)["content"] == "hello"


class TestAuthorizeOutcomes:
    def test_authorize_returns_wait_then_allow(self, factory: Any) -> None:
        gate = PermissionGate(factory=factory)
        req = ApprovalRequest(
            requested_action="shell echo",
            risk=Risk.HIGH,
            scope="shell:echo",
            requester="generic",
        )
        waiting = gate.authorize(req)
        assert waiting.outcome is Outcome.WAIT
        assert waiting.approval_id
        gate.decide(waiting.approval_id, approve=True, policy=Policy.ALLOW_SESSION)
        allowed = gate.authorize(req)
        assert allowed.outcome is Outcome.ALLOW

    def test_authorize_denies_default_deny_scope(self, factory: Any) -> None:
        gate = PermissionGate(factory=factory)
        decision = gate.authorize(
            ApprovalRequest(
                requested_action="pay",
                risk=Risk.CRITICAL,
                scope="browser:transact",
                requester="BrowserAgent",
            )
        )
        assert decision.outcome is Outcome.DENY
        assert decision.approval_id


class TestSchemaValidationBeforeExecution:
    """Invalid arguments never reach a handler."""

    @staticmethod
    def _spy_registry(calls: list[dict[str, Any]]) -> Any:
        settings = SimpleNamespace(tools_require_approval=False, tools_plugin_dir="")
        del settings
        from agent_system.services.tools.registry import Tool, ToolRegistry

        def handler(args: dict[str, Any], _ctx: Any) -> dict[str, Any]:
            calls.append(args)
            return {"ok": True}

        registry = ToolRegistry()
        registry.register(
            Tool(
                name="probe",
                description="probe",
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "minLength": 2},
                        "count": {"type": "integer", "minimum": 1},
                        "mode": {"type": "string", "enum": ["a", "b"]},
                        "nested": {
                            "type": "object",
                            "properties": {"deep": {"type": "boolean"}},
                            "required": ["deep"],
                            "additionalProperties": False,
                        },
                        "items": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
                risk="read",
                handler=handler,
            )
        )
        return registry

    def test_missing_required_argument(self) -> None:
        calls: list[dict[str, Any]] = []
        registry = self._spy_registry(calls)
        tool = registry.get("probe")
        assert tool is not None
        ctx = ToolContext(settings=SimpleNamespace(tools_require_approval=False))
        with pytest.raises(Exception) as exc:
            execute_tool(tool, {}, ctx)
        assert "missing required argument 'name'" in str(exc.value)
        assert calls == []

    def test_unknown_argument_rejected(self) -> None:
        errors = validate_arguments(
            "probe",
            {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "additionalProperties": False,
            },
            {"name": "ok", "surprise": 1},
        )
        assert any("unknown argument" in error for error in errors)

    def test_wrong_type(self) -> None:
        errors = validate_arguments(
            "probe", {"type": "object", "properties": {"n": {"type": "integer"}}}, {"n": "5"}
        )
        assert any("expected integer" in error for error in errors)

    def test_malformed_nested_object(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "nested": {
                    "type": "object",
                    "properties": {"deep": {"type": "boolean"}},
                    "required": ["deep"],
                }
            },
        }
        errors = validate_arguments("probe", schema, {"nested": {"deep": "yes"}})
        assert any("expected boolean" in error for error in errors)

    def test_invalid_enum(self) -> None:
        errors = validate_arguments(
            "probe",
            {"type": "object", "properties": {"mode": {"type": "string", "enum": ["a"]}}},
            {"mode": "z"},
        )
        assert any("expected one of" in error for error in errors)

    def test_oversized_input(self) -> None:
        errors = validate_arguments("probe", {"type": "object"}, {"blob": "x" * 200_000})
        assert any("character limit" in error for error in errors)

    def test_malicious_path_is_refused_before_the_handler(
        self, factory: Any, tmp_path: Path
    ) -> None:
        ctx = _ctx(factory, tmp_path, PermissionGate(factory=factory), tools_require_approval=False)
        tool = build_registry(ctx.settings).get("file_read")
        assert tool is not None
        for attempt in ("../../etc/passwd", "/etc/shadow", "\x00evil", "~/.ssh/id_rsa"):
            with pytest.raises(Exception) as exc:
                execute_tool(tool, {"path": attempt}, ctx)
            message = str(exc.value)
            assert "outside allowed roots" in message or "refusing" in message

    def test_symlink_escape_is_refused(self, factory: Any, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside-secret.txt"
        outside.write_text("classified", encoding="utf-8")
        link = tmp_path / "link.txt"
        link.symlink_to(outside)
        ctx = _ctx(factory, tmp_path, PermissionGate(factory=factory), tools_require_approval=False)
        tool = build_registry(ctx.settings).get("file_read")
        assert tool is not None
        with pytest.raises(Exception) as exc:
            execute_tool(tool, {"path": str(link)}, ctx)
        assert "outside allowed roots" in str(exc.value)


class TestExpiryIsFailClosed:
    def test_expired_grant_denies(self, factory: Any) -> None:
        from datetime import timedelta

        gate = PermissionGate(factory=factory)
        record = gate.request(
            ApprovalRequest(
                requested_action="x", risk=Risk.LOW, scope="file:write", requester="agent"
            )
        )
        gate.decide(record.approval_id, approve=True, policy=Policy.ALLOW_SESSION)
        # Backdate the expiry in the store, then confirm the gate fails closed.
        gate._records[record.approval_id].expires_at = utcnow() - timedelta(minutes=1)  # noqa: SLF001
        from agent_system.infra.db import session_scope
        from agent_system.infra.models import Approval as ApprovalRow

        with session_scope(factory) as db:
            row = db.get(ApprovalRow, record.approval_id)
            assert row is not None
            row.expires_at = utcnow() - timedelta(minutes=1)
        fresh = PermissionGate(factory=factory)
        allowed, _ = fresh.check(
            ApprovalRequest(
                requested_action="x", risk=Risk.LOW, scope="file:write", requester="agent"
            )
        )
        assert not allowed
