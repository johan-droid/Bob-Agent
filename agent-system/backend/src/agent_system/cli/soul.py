"""agentctl soul — view and edit Bob's identity (SOUL.md).

The soul is injected as the `<identity>` block on every model call. All
commands are local file operations (no backend needed).
"""

from __future__ import annotations

import os
import subprocess

import typer
from rich.console import Console
from rich.markdown import Markdown

from agent_system.services.soul import find_soul, load_soul

soul_app = typer.Typer(help="View and edit the agent soul (SOUL.md)", no_args_is_help=True)
_console = Console()


@soul_app.command("show")
def soul_show(
    raw: bool = typer.Option(False, "--raw", help="Plain text instead of rendered markdown"),
) -> None:
    """Show the active soul file."""
    path, text = load_soul()
    if path is None or not text:
        _console.print("[yellow]no SOUL.md found.[/yellow]")
        _console.print("[dim]Create one at ./SOUL.md, or point SOUL_PATH at it.[/dim]")
        raise typer.Exit(code=1)
    _console.print(f"[dim]{path}[/dim]\n")
    if raw:
        typer.echo(text)
    else:
        _console.print(Markdown(text))


@soul_app.command("path")
def soul_path() -> None:
    """Print the resolved soul file path."""
    path = find_soul()
    if path is None:
        _console.print("[yellow]no SOUL.md found.[/yellow]")
        raise typer.Exit(code=1)
    typer.echo(str(path))


@soul_app.command("edit")
def soul_edit() -> None:
    """Open the soul file in $EDITOR (creates ./SOUL.md if none exists)."""
    path = find_soul()
    if path is None:
        from pathlib import Path

        path = Path.cwd() / "SOUL.md"
        path.write_text("# SOUL.md — Who Bob Is\n\nYou are Bob.\n", encoding="utf-8")
    editor = os.environ.get("EDITOR", "vi")
    try:
        rc = subprocess.run([editor, str(path)]).returncode
    except FileNotFoundError:
        _console.print(f"[red]editor '{editor}' not found — set $EDITOR.[/red]")
        raise typer.Exit(code=2) from None
    if rc != 0:
        raise typer.Exit(code=rc)
