"""agentctl skills — manage Hermes-style agent skills via the API.

All commands go through ``api_request`` (same /api/v1 contracts as the
dashboard, per §34): no CLI-only business logic. Skills themselves can be
created by agents too, through ``POST /api/v1/skills``.
"""

from __future__ import annotations

from typing import Any

import typer

skills_app = typer.Typer(help="Manage agent skills", no_args_is_help=True)

# Module-level Typer defaults (B008: no function calls in argument defaults).
_SET_NEW_OPTION: Any = typer.Option(None, "--set", help="Config KEY=VALUE")
_SET_CONFIG_OPTION: Any = typer.Option(None, "--set", help="Set KEY=VALUE")


def _api() -> Any:
    """Lazy access to main's HTTP layer (avoids a main<->skills import cycle)."""
    from agent_system.cli import main as _main

    return _main


def _command(fn: Any) -> Any:
    """Apply main's exit-code wrapper at call time (import-safe decorator)."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        from agent_system.cli.main import run_command

        return run_command(fn)(*args, **kwargs)

    return wrapper


def _parse_kv(pairs: list[str]) -> dict[str, Any]:
    """Parse KEY=VALUE options (values attempted as YAML scalars)."""
    import yaml

    out: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise typer.BadParameter(f"expected KEY=VALUE, got '{pair}'")
        key, _, raw = pair.partition("=")
        try:
            out[key] = yaml.safe_load(raw)
        except Exception:
            out[key] = raw
    return out


@skills_app.command("list")
@_command
def skills_list(
    agent: str | None = typer.Option(None, "--agent", help="Filter to an agent type"),
) -> Any:
    """List skills (summaries)."""
    params = {"agent_type": agent} if agent else {}
    data = _api().api_request("GET", "/api/v1/skills", params=params)
    if isinstance(data, dict) and not _json_mode():
        from rich import box
        from rich.console import Console
        from rich.table import Table

        table = Table(box=box.SIMPLE_HEAVY, title="[bold]Skills[/bold]", title_justify="left")
        table.add_column("Name", style="bold")
        table.add_column("Version", style="dim")
        table.add_column("On", justify="center")
        table.add_column("Agents", style="dim")
        table.add_column("Description", style="dim", overflow="fold")
        for skill in data.get("skills", []):
            agents = ",".join(skill.get("agents", [])) or "all"
            mark = "[green]●[/]" if skill.get("enabled") else "[dim]○[/]"
            table.add_row(
                skill["name"], skill.get("version", ""), mark, agents, skill.get("description", "")
            )
        Console().print(table)
        for err in data.get("errors", []):
            typer.secho(f"! {err}", fg="yellow", err=True)
        return None
    _api().output(data)
    return None


@skills_app.command("show")
@_command
def skills_show(name: str = typer.Argument(...)) -> Any:
    """Show a skill with instructions + effective config."""
    data = _api().api_request("GET", f"/api/v1/skills/{name}")
    if isinstance(data, dict) and not _json_mode():
        typer.echo(f"# {data['name']} v{data.get('version', '')}")
        typer.echo(data.get("description", ""))
        typer.echo(
            f"enabled: {data.get('enabled')}   agents: {','.join(data.get('agents', [])) or 'all'}"
        )
        typer.echo(f"config: {data.get('config', {})}")
        typer.echo("---")
        typer.echo(data.get("instructions", ""))
        return None
    _api().output(data)
    return None


@skills_app.command("new")
@_command
def skills_new(
    name: str = typer.Argument(..., help="Skill slug: lowercase, digits, dashes"),
    description: str = typer.Option(..., "--description", "-d", help="What the skill teaches"),
    instructions: str | None = typer.Option(None, "--instructions", help="Instruction body"),
    file: str | None = typer.Option(None, "--file", help="Read instructions from a file"),
    agents: str = typer.Option("", "--agents", help="Comma-separated agent types (empty = all)"),
    set_values: list[str] | None = _SET_NEW_OPTION,
    author: str = typer.Option("user", "--author", help="Who creates it (agents use agent:<type>)"),
) -> Any:
    """Scaffold a new skill (users and agents)."""
    body = instructions
    if file:
        body = open(file, encoding="utf-8").read()
    if not body:
        raise typer.BadParameter("provide --instructions or --file")
    agent_list = [a.strip() for a in agents.split(",") if a.strip()]
    created = _api().api_request(
        "POST",
        "/api/v1/skills",
        json={
            "name": name,
            "description": description,
            "instructions": body,
            "agents": agent_list,
            "config": _parse_kv(set_values or []),
            "author": author,
        },
    )
    _api().output({"created": created["name"], "version": created.get("version")})
    return None


@skills_app.command("add")
@_command
def skills_add(
    source: str = typer.Argument(..., help="Local path, .md URL, or git URL"),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace on name conflict"),
) -> Any:
    """Import skill(s) from a path or URL."""
    data = _api().api_request(
        "POST", "/api/v1/skills/import", json={"source": source, "overwrite": overwrite}
    )
    _api().output({"added": [s["name"] for s in data.get("skills", [])]})
    return None


@skills_app.command("enable")
@_command
def skills_enable(name: str = typer.Argument(...)) -> Any:
    """Enable a skill."""
    data = _api().api_request("PATCH", f"/api/v1/skills/{name}", json={"enabled": True})
    _api().output({"enabled": data["name"]})
    return None


@skills_app.command("disable")
@_command
def skills_disable(name: str = typer.Argument(...)) -> Any:
    """Disable a skill (kept on disk, never injected)."""
    data = _api().api_request("PATCH", f"/api/v1/skills/{name}", json={"enabled": False})
    _api().output({"disabled": data["name"]})
    return None


@skills_app.command("config")
@_command
def skills_config(
    name: str = typer.Argument(...),
    key: str | None = typer.Argument(None, help="Show one key (omit to show all)"),
    set_values: list[str] | None = _SET_CONFIG_OPTION,
) -> Any:
    """Show or set a skill's config values."""
    if set_values:
        data = _api().api_request(
            "PATCH", f"/api/v1/skills/{name}", json={"config": _parse_kv(set_values)}
        )
        _api().output({f"{name}.config": data.get("config", {})})
        return None
    data = _api().api_request("GET", f"/api/v1/skills/{name}")
    config = data.get("config", {})
    _api().output({key: config.get(key)} if key else {f"{name}.config": config})
    return None


@skills_app.command("rm")
@_command
def skills_rm(name: str = typer.Argument(...)) -> Any:
    """Delete a skill entirely."""
    _api().api_request("DELETE", f"/api/v1/skills/{name}")
    _api().output({"deleted": name})
    return None


def _json_mode() -> bool:
    from agent_system.cli.main import _state

    return bool(_state.get("json"))
