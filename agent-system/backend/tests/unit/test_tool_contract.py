"""Unit — Phase 1 tool contract.

Pins the properties the harness builds on, before any harness behaviour exists:

- ``ToolLifecycle`` is a real state machine: invalid transitions raise, the
  terminal states stay terminal, and every state has an explicit row;
- ``CapabilityMetadata`` is a code-free projection of a capability with the
  derived policy (risk level, approval requirement, default-deny) precomputed;
- ``ExecutionRequest`` carries provenance from both the parsed call and the
  execution context;
- ``decide()`` is pure policy — it never touches the gate and never raises;
- ``execute_request()`` returns an ``ExecutionResult`` value for *every*
  terminal path and still delegates to the one execution seam (``execute_tool``).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from agent_system.services.permissions import CapabilityRisk, PermissionGate, Risk
from agent_system.services.tool_errors import ToolError
from agent_system.services.tools import Tool, ToolContext
from agent_system.services.tools.contract import (
    TRANSITIONS,
    CapabilityMetadata,
    ExecutionDecision,
    ExecutionOutcome,
    ExecutionRequest,
    ExecutionResult,
    ToolLifecycle,
    ToolLifecycleError,
    can_transition,
    is_terminal,
    validate_transition,
)
from agent_system.services.tools.execution import decide, execute_request
from agent_system.services.tools.protocol import ProtocolKind, ToolCall, parse_tool_calls


def _settings(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {"tools_require_approval": True, "tools_shell_mode": "off"}
    base.update(overrides)
    return SimpleNamespace(**base)


def _tool(
    name: str = "demo",
    *,
    risk: str = "read",
    scope: Any = None,
    handler: Any = None,
    requires_approval: bool | None = None,
    destructive_reason: str | None = None,
    timeout_seconds: int | None = None,
) -> Tool:
    return Tool(
        name=name,
        description=f"{name} capability",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        risk=risk,
        handler=handler or (lambda args, ctx: {"echo": args.get("value")}),
        scope=scope,
        requires_approval=requires_approval,
        destructive_reason=destructive_reason,
        timeout_seconds=timeout_seconds,
    )


def _request(tool: str = "demo", arguments: dict[str, Any] | None = None) -> ExecutionRequest:
    return ExecutionRequest.for_tool(tool, arguments if arguments is not None else {"value": "x"})


class TestToolLifecycle:
    def test_every_state_has_an_explicit_row(self) -> None:
        assert set(TRANSITIONS) == set(ToolLifecycle)

    def test_happy_path(self) -> None:
        path = [
            ToolLifecycle.REQUESTED,
            ToolLifecycle.VALIDATED,
            ToolLifecycle.EXECUTING,
            ToolLifecycle.SUCCEEDED,
        ]
        for current, target in zip(path, path[1:], strict=False):
            validate_transition(current, target)

    def test_approval_path(self) -> None:
        validate_transition(ToolLifecycle.VALIDATED, ToolLifecycle.AWAITING_APPROVAL)
        validate_transition(ToolLifecycle.AWAITING_APPROVAL, ToolLifecycle.EXECUTING)
        validate_transition(ToolLifecycle.EXECUTING, ToolLifecycle.SUCCEEDED)

    def test_approval_can_be_rejected_instead(self) -> None:
        validate_transition(ToolLifecycle.AWAITING_APPROVAL, ToolLifecycle.REJECTED)

    def test_skipping_validation_raises(self) -> None:
        with pytest.raises(ToolLifecycleError) as exc:
            validate_transition(ToolLifecycle.REQUESTED, ToolLifecycle.EXECUTING)
        assert "REQUESTED -> EXECUTING" in str(exc.value)

    def test_execution_cannot_go_back_to_requested(self) -> None:
        with pytest.raises(ToolLifecycleError):
            validate_transition(ToolLifecycle.EXECUTING, ToolLifecycle.REQUESTED)

    def test_terminal_states_stay_terminal(self) -> None:
        for state in (ToolLifecycle.REJECTED, ToolLifecycle.SUCCEEDED, ToolLifecycle.FAILED):
            assert is_terminal(state)
            assert TRANSITIONS[state] == frozenset()
            for target in ToolLifecycle:
                assert not can_transition(state, target)

    def test_non_terminal_states_are_not_terminal(self) -> None:
        for state in (
            ToolLifecycle.REQUESTED,
            ToolLifecycle.VALIDATED,
            ToolLifecycle.AWAITING_APPROVAL,
            ToolLifecycle.EXECUTING,
        ):
            assert not is_terminal(state)

    def test_can_transition_never_raises(self) -> None:
        assert can_transition(ToolLifecycle.REQUESTED, ToolLifecycle.VALIDATED)
        assert not can_transition(ToolLifecycle.SUCCEEDED, ToolLifecycle.FAILED)


class TestCapabilityMetadata:
    def test_read_capability_is_allowed_and_low_risk(self) -> None:
        metadata = CapabilityMetadata.from_tool(_tool(risk="read"), _settings())
        assert metadata.capability_risk is CapabilityRisk.READ
        assert metadata.risk_level is Risk.LOW
        assert not metadata.requires_approval
        assert not metadata.default_deny

    def test_write_capability_requires_approval(self) -> None:
        metadata = CapabilityMetadata.from_tool(_tool(risk="write"), _settings())
        assert metadata.capability_risk is CapabilityRisk.WRITE
        assert metadata.risk_level is Risk.MEDIUM
        assert metadata.requires_approval
        assert not metadata.default_deny

    def test_write_is_allowed_when_approvals_are_disabled(self) -> None:
        metadata = CapabilityMetadata.from_tool(
            _tool(risk="write"), _settings(tools_require_approval=False)
        )
        assert not metadata.requires_approval

    def test_explicit_requires_approval_overrides_the_setting(self) -> None:
        # The override applies to write/execute tiers; read is always allowed.
        metadata = CapabilityMetadata.from_tool(
            _tool(risk="write", requires_approval=False), _settings()
        )
        assert not metadata.requires_approval

    def test_read_tier_cannot_be_made_to_require_approval(self) -> None:
        metadata = CapabilityMetadata.from_tool(
            _tool(risk="read", requires_approval=True), _settings()
        )
        assert not metadata.requires_approval

    def test_destructive_tier_is_default_deny(self) -> None:
        metadata = CapabilityMetadata.from_tool(
            _tool(risk="destructive", destructive_reason="deletes user data"),
            _settings(tools_require_approval=False),
        )
        assert metadata.default_deny
        assert metadata.risk_level is Risk.CRITICAL
        assert metadata.destructive_reason == "deletes user data"

    def test_dangerous_scope_escalates_even_a_read_tier(self) -> None:
        metadata = CapabilityMetadata.from_tool(_tool(risk="read", scope="host:shell"), _settings())
        assert metadata.default_deny
        assert metadata.risk_level is Risk.CRITICAL

    def test_callable_scope_reports_the_capability_name(self) -> None:
        metadata = CapabilityMetadata.from_tool(
            _tool(name="file_read", scope=lambda args: f"file:{args['value']}"), _settings()
        )
        assert metadata.scope == "file_read"

    def test_unknown_tier_fails_safe_to_execute(self) -> None:
        metadata = CapabilityMetadata.from_tool(_tool(risk="nonsense"), _settings())
        assert metadata.capability_risk is CapabilityRisk.EXECUTE
        assert metadata.risk_level is Risk.HIGH

    def test_metadata_never_carries_the_handler(self) -> None:
        metadata = CapabilityMetadata.from_tool(_tool(), _settings())
        assert "handler" not in metadata.as_dict()

    def test_timeout_and_group_are_projected(self) -> None:
        metadata = CapabilityMetadata.from_tool(_tool(timeout_seconds=42), _settings())
        assert metadata.timeout_seconds == 42
        assert metadata.group == "builtin"

    def test_as_dict_is_json_serializable(self) -> None:
        payload = CapabilityMetadata.from_tool(_tool(risk="write"), _settings()).as_dict()
        assert json.loads(json.dumps(payload))["capability_risk"] == "write"


class TestExecutionRequest:
    def test_from_tool_call_carries_provenance(self) -> None:
        calls = parse_tool_calls({"output": '```tool:demo\n{"value": "hi"}\n```'})
        ctx = SimpleNamespace(
            session_id="ses_1",
            task_id="task_1",
            agent_run_id="run_1",
            agent_type="code",
            workspace_id="ws_1",
        )
        request = ExecutionRequest.from_tool_call(calls[0], ctx)
        assert request.call_id == calls[0].id
        assert request.tool == "demo"
        assert request.arguments == {"value": "hi"}
        assert request.source == calls[0].source
        assert request.protocol == "bob_fenced"
        assert (request.session_id, request.task_id, request.agent_run_id) == (
            "ses_1",
            "task_1",
            "run_1",
        )
        assert (request.agent_type, request.workspace_id) == ("code", "ws_1")

    def test_from_tool_call_without_context_is_honest(self) -> None:
        call = ToolCall(
            id="call_1",
            name="demo",
            arguments={},
            source="test",
            protocol=ProtocolKind.FENCED,
        )
        request = ExecutionRequest.from_tool_call(call)
        assert request.session_id is None
        assert request.workspace_id is None
        assert request.protocol == "bob_fenced"

    def test_for_tool_generates_a_prefixed_call_id(self) -> None:
        request = ExecutionRequest.for_tool("demo", {"value": "x"})
        assert request.call_id.startswith("toolcall_")
        assert request.source == "internal"
        assert request.protocol is None

    def test_for_tool_preserves_an_explicit_call_id(self) -> None:
        request = ExecutionRequest.for_tool("demo", call_id="call_fixed")
        assert request.call_id == "call_fixed"

    def test_arguments_are_copied_not_aliased(self) -> None:
        args = {"value": "x"}
        request = ExecutionRequest.for_tool("demo", args)
        args["value"] = "mutated"
        assert request.arguments == {"value": "x"}

    def test_as_dict_is_json_serializable(self) -> None:
        request = ExecutionRequest.for_tool("demo", {"value": "x"})
        assert json.loads(json.dumps(request.as_dict()))["tool"] == "demo"


class TestDecide:
    """The pure policy verdict: no gate side effects, no exceptions."""

    def test_read_is_allowed(self) -> None:
        decision = decide(_tool(risk="read"), _request(), _settings())
        assert decision.outcome is ExecutionOutcome.ALLOW
        assert decision.allowed
        assert not decision.refused
        assert decision.scope == "demo"

    def test_write_awaits_approval(self) -> None:
        decision = decide(_tool(risk="write"), _request(), _settings())
        assert decision.outcome is ExecutionOutcome.AWAIT_APPROVAL
        assert decision.needs_approval
        assert decision.approval_id is None  # nothing was requested yet

    def test_write_is_allowed_when_approvals_are_disabled(self) -> None:
        decision = decide(_tool(risk="write"), _request(), _settings(tools_require_approval=False))
        assert decision.outcome is ExecutionOutcome.ALLOW

    def test_destructive_is_denied(self) -> None:
        decision = decide(
            _tool(risk="destructive", destructive_reason="rw"), _request(), _settings()
        )
        assert decision.outcome is ExecutionOutcome.DENY
        assert decision.refused
        assert decision.reason == "rw"

    def test_dangerous_scope_is_denied(self) -> None:
        decision = decide(_tool(risk="read", scope="credential:transmit"), _request(), _settings())
        assert decision.outcome is ExecutionOutcome.DENY
        assert decision.scope == "credential:transmit"

    def test_invalid_arguments_are_rejected_before_policy(self) -> None:
        decision = decide(_tool(risk="read"), _request(arguments={}), _settings())
        assert decision.outcome is ExecutionOutcome.INVALID
        assert decision.errors
        assert "value" in decision.errors[0]

    def test_callable_scope_is_resolved_per_call(self) -> None:
        tool = _tool(name="file_read", scope=lambda args: f"file:{args['value']}")
        decision = decide(tool, _request(arguments={"value": "a.txt"}), _settings())
        assert decision.scope == "file:a.txt"

    def test_next_state_maps_to_the_lifecycle(self) -> None:
        allow = decide(_tool(risk="read"), _request(), _settings())
        wait = decide(_tool(risk="write"), _request(), _settings())
        deny = decide(_tool(risk="destructive"), _request(), _settings())
        invalid = decide(_tool(), _request(arguments={}), _settings())
        assert allow.next_state is ToolLifecycle.EXECUTING
        assert wait.next_state is ToolLifecycle.AWAITING_APPROVAL
        assert deny.next_state is ToolLifecycle.REJECTED
        assert invalid.next_state is ToolLifecycle.REJECTED

    def test_decision_exposes_the_capability_policy(self) -> None:
        decision = decide(_tool(risk="write"), _request(), _settings())
        assert decision.capability_risk is CapabilityRisk.WRITE
        assert decision.risk_level is Risk.MEDIUM
        assert decision.capability.requires_approval

    def test_as_dict_is_json_serializable(self) -> None:
        decision = decide(_tool(risk="write"), _request(), _settings())
        payload = json.loads(json.dumps(decision.as_dict()))
        assert payload["outcome"] == "await_approval"
        assert payload["next_state"] == "AWAITING_APPROVAL"

    def test_hand_built_decision_with_outcome_keeps_capability(self) -> None:
        decision = decide(_tool(risk="write"), _request(), _settings())
        restated = decision.with_outcome(ExecutionOutcome.ALLOW, "granted")
        assert restated.allowed
        assert restated.capability is decision.capability
        assert restated.scope == decision.scope


class TestExecuteRequest:
    """Every terminal path is a value, never an exception."""

    def test_read_call_succeeds(self) -> None:
        ctx = ToolContext(settings=_settings())
        result = execute_request(_tool(risk="read"), _request(), ctx)
        assert result.ok
        assert result.lifecycle is ToolLifecycle.SUCCEEDED
        assert result.terminal
        assert result.output == {"echo": "x"}
        assert result.decision.outcome is ExecutionOutcome.ALLOW
        assert result.model_payload == {"echo": "x"}
        assert result.duration_ms is not None and result.duration_ms >= 0

    def test_invalid_arguments_are_rejected_without_running_the_handler(self) -> None:
        ran: list[bool] = []
        tool = _tool(risk="read", handler=lambda args, ctx: ran.append(True) or {})
        result = execute_request(tool, _request(arguments={}), ToolContext(settings=_settings()))
        assert result.refused
        assert result.lifecycle is ToolLifecycle.REJECTED
        assert result.decision.outcome is ExecutionOutcome.INVALID
        assert result.error is not None
        assert result.error["error"] == "invalid_arguments"
        assert ran == []

    def test_write_pauses_for_approval(self) -> None:
        ran: list[bool] = []
        tool = _tool(risk="write", handler=lambda args, ctx: ran.append(True) or {})
        ctx = ToolContext(settings=_settings(), gate=PermissionGate())
        result = execute_request(tool, _request(), ctx)
        assert result.refused
        assert result.awaiting_approval
        assert result.lifecycle is ToolLifecycle.REJECTED
        assert result.decision.outcome is ExecutionOutcome.AWAIT_APPROVAL
        assert result.decision.approval_id
        assert result.error is not None and result.error["error"] == "needs_approval"
        assert ran == []

    def test_destructive_is_refused(self) -> None:
        tool = _tool(risk="destructive", destructive_reason="rm -rf")
        result = execute_request(tool, _request(), ToolContext(settings=_settings()))
        assert result.refused
        assert result.decision.outcome is ExecutionOutcome.DENY
        assert result.model_payload["error"] == "permission_denied"
        assert result.model_payload["reason"] == "rm -rf"

    def test_tool_error_is_a_failure_not_a_crash(self) -> None:
        def boom(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            raise ToolError("handler exploded")

        result = execute_request(_tool(handler=boom), _request(), ToolContext(settings=_settings()))
        assert not result.ok
        assert result.lifecycle is ToolLifecycle.FAILED
        assert result.error == {"error": "tool_error", "tool": "demo", "detail": "handler exploded"}

    def test_capability_crash_is_contained(self) -> None:
        def boom(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            raise RuntimeError("segfault-ish")

        result = execute_request(_tool(handler=boom), _request(), ToolContext(settings=_settings()))
        assert result.lifecycle is ToolLifecycle.FAILED
        assert result.error is not None
        assert result.error["error"] == "capability_crash"
        assert "RuntimeError" in str(result.error["detail"])

    def test_error_payload_marks_the_call_failed(self) -> None:
        tool = _tool(handler=lambda args, ctx: {"error": "nope", "tool": "demo"})
        result = execute_request(tool, _request(), ToolContext(settings=_settings()))
        assert not result.ok
        assert result.lifecycle is ToolLifecycle.FAILED
        assert result.model_payload == {"error": "nope", "tool": "demo"}

    def test_existing_grant_restates_the_decision_as_allowed(self) -> None:
        tool = _tool(risk="write")
        gate = PermissionGate()
        ctx = ToolContext(settings=_settings(), gate=gate)
        pending = execute_request(tool, _request(), ctx)
        assert pending.decision.approval_id
        gate.decide(pending.decision.approval_id, approve=True)
        granted = execute_request(tool, _request(), ctx)
        assert granted.ok
        assert granted.decision.outcome is ExecutionOutcome.ALLOW
        assert granted.decision.reason == "allowed by an existing grant"

    def test_result_decision_is_a_contract_type(self) -> None:
        result = execute_request(_tool(), _request(), ToolContext(settings=_settings()))
        assert isinstance(result.decision, ExecutionDecision)

    def test_as_dict_is_json_serializable(self) -> None:
        result = execute_request(_tool(), _request(), ToolContext(settings=_settings()))
        payload = json.loads(json.dumps(result.as_dict()))
        assert payload["lifecycle"] == "SUCCEEDED"
        assert payload["request"]["tool"] == "demo"
        assert payload["decision"]["outcome"] == "allow"

    def test_tool_and_call_id_mirror_the_request(self) -> None:
        request = _request(tool="demo")
        result = execute_request(_tool(), request, ToolContext(settings=_settings()))
        assert result.tool == request.tool
        assert result.call_id == request.call_id
        assert isinstance(result, ExecutionResult)
