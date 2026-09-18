"""Invariants for the policy-aware execution paths (INV-002).

These pin the structural ordering contract the audit demanded:

    UNTRUSTED MODEL ARGUMENTS
        -> SCHEMA VALIDATION
        -> NORMALIZED ARGUMENTS
        -> SCOPE DERIVATION
        -> POLICY / APPROVAL
        -> EXECUTION

For both :func:`execute_with_policy` and :func:`execute_request_with_policy`,
malformed arguments must:

- be refused *before* policy evaluation (a spy engine must never run);
- never reach scope derivation (a spy scope must never run);
- never enter the handler (a counting handler must stay at zero);
- produce a deterministic, typed refusal (``ToolValidationError`` /
  ``ExecutionResult`` refused as ``INVALID``) — never a ``KeyError``,
  ``TypeError``, or a bare 500-style crash.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_system.services.policy import (
    PolicyContext,
    PolicyEngine,
    PolicyVerdict,
)
from agent_system.services.tool_errors import ToolValidationError
from agent_system.services.tools.contract import ExecutionOutcome
from agent_system.services.tools.execution import (
    execute_request_with_policy,
    execute_with_policy,
)
from agent_system.services.tools.registry import Tool, ToolContext
from agent_system.services.tools.schemas import MAX_ARGUMENT_CHARS

# PolicyContext forward-references Tool; resolve before constructing (pydantic
# refuses an un-rebuilt model at class construction time).
PolicyContext.model_rebuild()

PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1},
        "mode": {"type": "string", "enum": ["alpha", "beta"]},
        "nested": {
            "type": "object",
            "properties": {"deep": {"type": "boolean"}},
            "required": ["deep"],
            "additionalProperties": False,
        },
    },
    "required": ["path"],
    "additionalProperties": False,
}


class _SpyScope:
    """A scope callable that fails the test if it ever runs.

    Scope derivation is the most fragile sink for malformed arguments; this
    proves it is structurally unreachable for invalid calls.
    """

    def __init__(self, owner: str) -> None:
        self._owner = owner

    def __call__(self, args: dict[str, Any]) -> str:
        raise AssertionError(f"scope derivation ran for {self._owner} with {args!r}")


class _SpyEngine:
    """A policy engine that fails the test if policy is evaluated."""

    def __init__(self, owner: str) -> None:
        self._owner = owner

    def evaluate(self, ctx: Any) -> Any:
        raise AssertionError(f"policy evaluated for {self._owner} with {ctx!r}")

    def evaluate_and_authorize(self, ctx: Any) -> Any:
        raise AssertionError(f"policy evaluated for {self._owner} with {ctx!r}")


def _settings(**overrides: Any) -> Any:
    from agent_system.config import Settings

    base: dict[str, Any] = {
        "tools_require_approval": False,
        "tools_shell_mode": "off",
        "heroku_jail": False,
        "max_execution_time_seconds": 1800,
    }
    base.update(overrides)
    return Settings(**base)


def _make_tool(ran: list[dict[str, Any]], *, risk: str = "read", scope: Any = None) -> Tool:
    return Tool(
        name="probe",
        description="probe",
        parameters=PARAMS,
        risk=risk,
        handler=lambda args, ctx: ran.append(args) or {"echo": args["path"]},
        scope=scope,
        group="test",
    )


def _ctx(settings: Any = None) -> ToolContext:
    ctx = ToolContext(
        settings=settings or _settings(),
        factory=None,
        session_id="ses_inv",
        task_id="task_inv",
        agent_run_id="run_inv",
        agent_type="test_agent",
        workspace_id="ws_inv",
    )
    ctx.workspace_path = "/tmp/ws"
    ctx.permissions = []
    ctx.environment = {}
    return ctx


MALFORMED_PAYLOADS: list[tuple[str, Any]] = [
    ("missing required field", {"mode": "alpha"}),
    ("null for required field", {"path": None}),
    ("wrong primitive", {"path": 42}),
    ("wrong nested primitive", {"path": "x", "nested": {"deep": "not-bool"}}),
    ("missing nested required", {"path": "x", "nested": {"mode": "alpha"}}),
    ("extra field on closed object", {"path": "x", "extra": 1}),
    ("extra field in closed nested object", {"path": "x", "nested": {"deep": True, "oops": 1}}),
    ("empty object", {}),
    ("unexpected enum", {"path": "x", "mode": "gamma"}),
    ("short string", {"path": ""}),
    ("string top-level", "just a string"),
    ("number top-level", 42),
    ("None top-level", None),
    ("list top-level", ["path"]),
    ("oversized payload", {"path": "x" * MAX_ARGUMENT_CHARS}),
    ("deeply nested object", {"path": "x", "nested": {"deep": True, "_": {"a": {"b": {"c": 1}}}}}),
]


class TestPolicyPathsRefuseMalformedArguments:
    """Adversarial matrix over the two policy-aware entry points."""

    @pytest.mark.parametrize("scenario,args", MALFORMED_PAYLOADS, ids=lambda v: str(v)[:40])
    def test_execute_with_policy_refuses_before_policy_scope_or_handler(
        self, scenario: str, args: Any
    ) -> None:
        ran: list[dict[str, Any]] = []
        tool = _make_tool(ran, scope=_SpyScope("execute_with_policy"))

        with pytest.raises(ToolValidationError) as exc_info:
            execute_with_policy(
                tool,
                args,
                _ctx(),
                engine=_SpyEngine("execute_with_policy"),  # type: ignore[arg-type]
            )

        assert exc_info.value.tool_name == "probe"
        assert exc_info.value.errors
        assert ran == []  # handler never entered

    @pytest.mark.parametrize("scenario,args", MALFORMED_PAYLOADS, ids=lambda v: str(v)[:40])
    def test_execute_request_with_policy_refuses_before_policy_scope_or_handler(
        self, scenario: str, args: Any
    ) -> None:
        from agent_system.services.tools.contract import ExecutionRequest

        ran: list[dict[str, Any]] = []
        tool = _make_tool(ran, scope=_SpyScope("execute_request_with_policy"))
        request = ExecutionRequest(call_id="inv-1", tool="probe", arguments=args)

        result = execute_request_with_policy(
            tool,
            request,
            _ctx(),
            engine=_SpyEngine("execute_request_with_policy"),  # type: ignore[arg-type]
        )

        assert result.refused
        assert result.decision.outcome is ExecutionOutcome.INVALID
        assert result.decision.errors
        assert result.error is not None
        assert result.error.get("error") == "invalid_arguments"
        assert result.error.get("tool") == "probe"
        assert ran == []  # handler never entered

    def test_execute_with_policy_rejects_oversized_serialization(self) -> None:
        ran: list[dict[str, Any]] = []
        tool = _make_tool(ran)

        with pytest.raises(ToolValidationError):
            execute_with_policy(
                tool,
                {"path": "x", "blob": "y" * MAX_ARGUMENT_CHARS},
                _ctx(),
                engine=_SpyEngine("oversize"),  # type: ignore[arg-type]
            )

        assert ran == []


class TestPolicyPathsStillExecuteValidCalls:
    """The ordering fix must not break the happy path (regression pin)."""

    def test_execute_with_policy_runs_read_tool(self) -> None:
        ran: list[dict[str, Any]] = []
        tool = _make_tool(ran, scope="test:read")
        engine = PolicyEngine(factory=None, settings=_settings())

        decision, output = execute_with_policy(tool, {"path": "/tmp/x"}, _ctx(), engine=engine)

        assert decision.verdict is PolicyVerdict.ALLOW
        assert output == {"echo": "/tmp/x"}
        assert len(ran) == 1

    def test_execute_request_with_policy_runs_read_tool(self) -> None:
        from agent_system.services.tools.contract import ExecutionRequest

        ran: list[dict[str, Any]] = []
        tool = _make_tool(ran, scope="test:read")
        engine = PolicyEngine(factory=None, settings=_settings())
        request = ExecutionRequest(call_id="inv-2", tool="probe", arguments={"path": "/tmp/x"})

        result = execute_request_with_policy(tool, request, _ctx(), engine=engine)

        assert result.ok
        assert result.decision.outcome is ExecutionOutcome.ALLOW
        assert result.output == {"echo": "/tmp/x"}
        assert len(ran) == 1

    def test_execute_with_policy_still_requires_approval_for_write(self) -> None:
        from agent_system.services.tool_errors import NeedsApprovalError

        ran: list[dict[str, Any]] = []
        tool = _make_tool(ran, risk="write", scope="test:write")
        engine = PolicyEngine(factory=None, settings=_settings(tools_require_approval=True))

        with pytest.raises(NeedsApprovalError):
            execute_with_policy(tool, {"path": "/tmp/x"}, _ctx(), engine=engine)

        assert ran == []
