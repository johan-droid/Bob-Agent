"""Capability library (v3.1 §14) — the one capability system.

Layout:

    services/tools/
        __init__.py    public API (this module)
        registry.py    Tool / ToolContext / ToolRegistry / build_registry
        schemas.py     runtime JSON-schema validation of arguments
        contract.py    the Phase 1 tool contract (request/decision/result/
                       metadata/lifecycle) — data only, no execution
        execution.py   the single validate -> authorize -> run path
        protocol.py    provider-native + fenced ToolCall protocols
        paths.py       workspace jail + scrubbing shared by capabilities
        optional.py    MCP / OpenConnector capabilities (when configured)
        builtin/       first-party capability groups
        plugins/       third-party capability loading

Compatibility: this package is the successor to the former
``services/tools.py`` module and re-exports its public names, so existing
``from agent_system.services.tools import ...`` imports keep working — they now
resolve to the capability library instead of a single-file module.
"""

from __future__ import annotations

from agent_system.services.tool_errors import (
    NeedsApprovalError,
    ToolError,
    ToolPermissionError,
    ToolValidationError,
)
from agent_system.services.tools.contract import (
    CapabilityMetadata,
    ExecutionDecision,
    ExecutionOutcome,
    ExecutionRequest,
    ExecutionResult,
    ToolLifecycle,
    ToolLifecycleError,
)
from agent_system.services.tools.execution import (
    PermissionPlan,
    authorize_tool,
    capability_metadata,
    decide,
    execute_request,
    execute_tool,
    plan_permission,
)
from agent_system.services.tools.registry import (
    Tool,
    ToolContext,
    ToolRegistry,
    build_registry,
)
from agent_system.services.tools.schemas import validate_arguments

#: Legacy risk-tier constants. They are the CapabilityRisk values, kept as
#: module-level strings because existing capability/plugin code imports them.
RISK_READ = "read"
RISK_WRITE = "write"
RISK_EXECUTE = "execute"
RISK_DESTRUCTIVE = "destructive"

__all__ = [
    "CapabilityMetadata",
    "ExecutionDecision",
    "ExecutionOutcome",
    "ExecutionRequest",
    "ExecutionResult",
    "NeedsApprovalError",
    "PermissionPlan",
    "RISK_DESTRUCTIVE",
    "RISK_EXECUTE",
    "RISK_READ",
    "RISK_WRITE",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolLifecycle",
    "ToolLifecycleError",
    "ToolPermissionError",
    "ToolRegistry",
    "ToolValidationError",
    "authorize_tool",
    "build_registry",
    "capability_metadata",
    "decide",
    "execute_request",
    "execute_tool",
    "plan_permission",
    "validate_arguments",
]
