"""Third-party capability plugins (folder-drop, same discovery as skills)."""

from __future__ import annotations

from agent_system.services.tools.plugins.manager import (
    ToolPlugin,
    ToolPluginError,
    ToolPluginManager,
    default_plugin_dir,
    load_plugin_tools,
)

__all__ = [
    "ToolPlugin",
    "ToolPluginError",
    "ToolPluginManager",
    "default_plugin_dir",
    "load_plugin_tools",
]
