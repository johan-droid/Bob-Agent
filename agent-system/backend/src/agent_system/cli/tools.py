"""agentctl tools — inspect the ReAct tool registry and toggle plugins.

``list`` shows built-in tools plus enabled plugin tools (same
``prompt_block()`` the model sees). ``enable``/``disable`` flip a plugin's
``.state.json`` toggle — the registry rebuilds on the next task, so no
restart is needed. Local file ops, same shape as the ``settings`` commands.
"""

from __future__ import annotations

from typing import Any

import typer
from rich import box
from rich.console import Console
from rich.table import Table

tools_app = typer.Typer(help="Inspect the tool registry and toggle tool plugins")


def _is_json() -> bool:
    from agent_system.cli.main import _state

    return bool(_state.get("json"))


def _emit(payload: Any) -> None:
    from agent_system.cli.main import output as _output

    _output(payload)


def _registry_payload() -> dict[str, Any]:
    from agent_system.cli.settings import effective
    from agent_system.services.tool_plugins import ToolPluginManager
    from agent_system.services.tools import build_registry

    settings = effective()
    registry = build_registry(settings)
    manager = ToolPluginManager(getattr(settings, "tools_plugin_dir", "tools_plugins"))
    plugins = [
        {
            "name": p.name,
            "risk": p.risk,
            "enabled": p.enabled,
            "description": p.description,
        }
        for p in manager.discover()
    ]
    tools = []
    for name in registry.names():
        tool = registry.get(name)
        tools.append({"name": name, "risk": tool.risk if tool else "?"})
    return {"tools": tools, "plugins": plugins, "errors": manager.last_errors}


@tools_app.command("list")
def tools_list() -> None:
    """List registry tools (built-ins + enabled plugins) and plugin state."""
    data = _registry_payload()
    if _is_json():
        _emit(data)
        return
    table = Table(box=box.SIMPLE_HEAVY, title="[bold]Tools[/bold]", title_justify="left")
    table.add_column("Name", style="bold")
    table.add_column("Risk", style="dim")
    for tool in data["tools"]:
        table.add_row(tool["name"], tool["risk"])
    Console().print(table)
    if data["plugins"]:
        ptable = Table(box=box.SIMPLE_HEAVY, title="[bold]Plugins[/bold]")
        ptable.add_column("Name", style="bold")
        ptable.add_column("Risk", style="dim")
        ptable.add_column("On", justify="center")
        ptable.add_column("Description", style="dim", overflow="fold")
        for plugin in data["plugins"]:
            mark = "[green]●[/]" if plugin["enabled"] else "[dim]○[/]"
            ptable.add_row(plugin["name"], plugin["risk"], mark, plugin["description"])
        Console().print(ptable)
    for err in data["errors"]:
        typer.secho(f"! {err}", fg="yellow", err=True)


@tools_app.command("enable")
def tools_enable(name: str = typer.Argument(..., help="Plugin name")) -> None:
    """Enable a tool plugin (effective on the next task, no restart)."""
    from agent_system.cli.settings import effective
    from agent_system.services.tool_plugins import ToolPluginError, ToolPluginManager

    settings = effective()
    manager = ToolPluginManager(getattr(settings, "tools_plugin_dir", "tools_plugins"))
    try:
        manager.set_enabled(name, True)
    except ToolPluginError as exc:
        typer.secho(f"error: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from exc
    if _is_json():
        _emit({"enabled": name})
        return
    typer.secho(f"✓ enabled {name}", fg="green")


@tools_app.command("disable")
def tools_disable(name: str = typer.Argument(..., help="Plugin name")) -> None:
    """Disable a tool plugin (removed from the next prompt, no restart)."""
    from agent_system.cli.settings import effective
    from agent_system.services.tool_plugins import ToolPluginError, ToolPluginManager

    settings = effective()
    manager = ToolPluginManager(getattr(settings, "tools_plugin_dir", "tools_plugins"))
    try:
        manager.set_enabled(name, False)
    except ToolPluginError as exc:
        typer.secho(f"error: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from exc
    if _is_json():
        _emit({"disabled": name})
        return
    typer.secho(f"✓ disabled {name}", fg="green")
