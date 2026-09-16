"""Capability registry — the one place capabilities are declared and listed.

A capability is a typed function the model can call. Every capability declares,
once, at registration time:

- ``risk``   its capability tier (``read`` | ``write`` | ``execute`` |
             ``destructive``) — the canonical mapping to LOW/MEDIUM/HIGH/
             CRITICAL lives in :mod:`agent_system.services.permissions`.
- ``scope``  the permission scope its calls fall under, either a constant or a
             function of the arguments (e.g. ``shell:/workspaces/x``).
- ``group``  which first-party capability group owns it (filesystem, coding,
             git, shell, browser, research, documents, memory, tasks, system).

Capabilities never evaluate permissions themselves: the single execution path
in :mod:`agent_system.services.tools.execution` validates arguments, resolves
the scope, and asks the permission gate.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_system.services.permissions import CapabilityRisk
from agent_system.services.tool_errors import ToolError

Handler = Callable[[dict[str, Any], Any], dict[str, Any]]


@dataclass
class ToolContext:
    """Everything a capability handler may need (no globals)."""

    settings: Any
    factory: Any = None
    session_id: str | None = None
    task_id: str | None = None
    agent_run_id: str | None = None
    agent_type: str | None = None
    workspace_id: str | None = None
    gate: Any = None
    emit: Callable[[str, dict[str, Any]], None] | None = None


@dataclass
class Tool:
    """One declared capability.

    Field order is part of the contract: existing call sites construct tools
    positionally as ``Tool(name, description, parameters, risk, handler)``.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    risk: str
    handler: Handler
    scope: str | Callable[[dict[str, Any]], str] | None = None
    requires_approval: bool | None = None
    group: str = "builtin"
    destructive_reason: str | None = None
    timeout_seconds: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    # -- derived policy -----------------------------------------------------

    @property
    def tier(self) -> CapabilityRisk:
        """Canonical capability tier (unknown values fail safe to execute)."""
        try:
            return CapabilityRisk(str(self.risk))
        except ValueError:
            return CapabilityRisk.EXECUTE

    def scope_for(self, args: dict[str, Any]) -> str:
        """Resolve this capability's permission scope for one call."""
        if callable(self.scope):
            try:
                return str(self.scope(args))
            except Exception:
                return self.name
        return str(self.scope or self.name)

    def permission_required(self, settings: Any = None) -> bool:
        """Whether this capability needs an approval to run.

        The documented decision table:

        ============= =========================================
        tier          decision
        ============= =========================================
        ``read``      allowed without approval
        ``write``     approval required (``tools_require_approval``)
        ``execute``   approval required (``tools_require_approval``)
        ``destructive`` default-deny — never approvable
        ============= =========================================

        A capability may override with an explicit ``requires_approval``; it
        cannot override ``destructive``.
        """
        if self.tier is CapabilityRisk.DESTRUCTIVE:
            return True
        if self.tier is CapabilityRisk.READ:
            return False
        if self.requires_approval is not None:
            return bool(self.requires_approval)
        return bool(getattr(settings, "tools_require_approval", True))

    def schema(self) -> dict[str, Any]:
        """The provider-facing schema (also what runtime validation uses)."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    def to_json(self) -> dict[str, Any]:
        """Inventory entry (capability docs + ``system`` introspection)."""
        return {
            "name": self.name,
            "description": self.description,
            "group": self.group,
            "risk": self.tier.value,
            "permission": (
                "deny (default-deny)"
                if self.tier is CapabilityRisk.DESTRUCTIVE
                else "approval required"
                if self.permission_required()
                else "allowed"
            ),
            "scope": self.scope if isinstance(self.scope, str) else self.name,
            "parameters": self.parameters,
        }


class ToolRegistry:
    """Name -> Tool, plus schema rendering for prompts."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ToolError(f"duplicate capability name '{tool.name}'")
        self._tools[tool.name] = tool

    def replace(self, tool: Tool) -> None:
        """Register or overwrite (used by tests and plugin overrides)."""
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def tools(self) -> list[Tool]:
        return [self._tools[name] for name in self.names()]

    def by_group(self, group: str) -> list[Tool]:
        return [t for t in self.tools() if t.group == group]

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self.tools()]

    def prompt_block(self) -> str:
        """Compact capability catalog rendered into the ReAct system prompt."""
        lines = [
            "You have these tools. To call one, emit a fenced block:",
            "```tool:<name>",
            '{"arg": "value"}',
            "```",
            "One call per block; you may emit several blocks per turn. "
            "Results come back as <tool_result> blocks — then continue reasoning.",
            "",
        ]
        for name in self.names():
            tool = self._tools[name]
            params = tool.parameters.get("properties", {})
            required = tool.parameters.get("required", [])
            sig = ", ".join(f"{key}{'' if key in required else '?'}" for key in params)
            marker = "[needs approval] " if tool.permission_required() else ""
            lines.append(f"- {name}({sig}): {marker}{tool.description}")
        return "\n".join(lines)


def _str_param(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def build_registry(settings: Any, plugin_dir: Path | str | None = None) -> ToolRegistry:
    """Assemble the capability library for these settings.

    First-party groups register in a fixed order so the prompt catalog and the
    inventory are deterministic. Optional groups (openconnector, MCP) appear
    only when configured; a misconfigured group never removes another group's
    capabilities. Enabled plugins merge last — a plugin may never shadow a
    first-party capability.
    """
    from agent_system.services.tools.builtin import register_first_party
    from agent_system.services.tools.optional import register_optional
    from agent_system.services.tools.plugins import ToolPluginManager, load_plugin_tools

    registry = ToolRegistry()
    register_first_party(registry, settings)
    for tool in register_optional(settings):
        if registry.get(tool.name) is None:
            registry.register(tool)
    resolved_plugin_dir: Path | str = plugin_dir or str(
        getattr(settings, "tools_plugin_dir", "tools_plugins")
    )
    manager = ToolPluginManager(resolved_plugin_dir)
    load_plugin_tools(manager, registry)
    return registry


__all__ = [
    "Handler",
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "build_registry",
    "ToolError",
    "_str_param",
]
