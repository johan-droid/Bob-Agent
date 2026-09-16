"""Plugin capability loading — facade over ``services/tool_plugins.py``.

The implementation (folder discovery, validation, sandboxed execution of
execute-risk plugins, per-plugin enable state) already worked and is preserved
where it is; this module is the documented entry point for the capability
library's ``plugins/`` layout and adds the one guarantee the library needs:

    a plugin may never shadow a first-party capability

Third-party capabilities stay strictly additive. They are also subject to the
same execution path as first-party ones: risk tier, approval gate, sandbox —
no exemptions, no plugin-local policy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_system.services.tool_plugins import (
    ToolPlugin,
    ToolPluginError,
    ToolPluginManager,
)

__all__ = ["ToolPlugin", "ToolPluginError", "ToolPluginManager", "load_plugin_tools"]


def load_plugin_tools(manager: ToolPluginManager, registry: Any) -> list[Any]:
    """Build enabled plugin capabilities, skipping any that shadow built-ins."""
    from agent_system.services.tools.registry import ToolRegistry

    assert isinstance(registry, ToolRegistry)  # noqa: S101 — internal contract
    added: list[Any] = []
    for tool in manager.build_tools():
        if registry.get(tool.name) is not None:
            manager.last_errors.append(f"{tool.name}: refusing to shadow a first-party capability")
            continue
        registry.register(tool)
        added.append(tool)
    return added


def default_plugin_dir(settings: Any) -> Path | str:
    return getattr(settings, "tools_plugin_dir", "tools_plugins")
