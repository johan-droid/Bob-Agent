"""agentctl backup — SQLite + vault + recordings snapshots.

Local-ops commands (same shape as ``settings``): backups touch local files
(the SQLite file, the vault, the recordings dir), so they run in-process via
``services/backup.py`` rather than over HTTP. ``restore`` is strict: it
requires ``--confirm`` and refuses when a live server answers on ``api_port``
unless ``--force-live`` is given.
"""

from __future__ import annotations

from typing import Any

import typer
from rich import box
from rich.console import Console
from rich.table import Table

backup_app = typer.Typer(help="Back up and restore SQLite + vault + recordings")


def _service() -> Any:
    from agent_system.cli.settings import effective
    from agent_system.services.backup import BackupService

    return BackupService(effective())


def _is_json() -> bool:
    from agent_system.cli.main import _state

    return bool(_state.get("json"))


def _emit(payload: Any) -> None:
    from agent_system.cli.main import output as _output

    _output(payload)


@backup_app.command("run")
def backup_run() -> None:
    """Take a backup now (SQLite online copy + vault/recordings snapshot)."""
    from agent_system.services.backup import BackupError

    try:
        record = _service().run()
    except BackupError as exc:
        typer.secho(f"backup failed: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from exc
    payload = {
        "backup_id": record.backup_id,
        "path": str(record.path),
        "db_bytes": record.db_bytes,
        "archive_bytes": record.archive_bytes,
        "skipped_secrets": record.skipped_secrets,
    }
    if _is_json():
        _emit(payload)
        return
    typer.secho(f"✓ backup {record.backup_id}", fg="green")
    typer.echo(f"  path: {record.path}")
    typer.echo(f"  db: {record.db_bytes} bytes  archive: {record.archive_bytes} bytes")
    if record.skipped_secrets:
        typer.echo(f"  skipped secret paths: {len(record.skipped_secrets)}")


@backup_app.command("list")
def backup_list() -> None:
    """List backups (newest last)."""
    records = _service().list_backups()
    payload = [
        {
            "backup_id": r.backup_id,
            "created_at": r.created_at,
            "db_bytes": r.db_bytes,
            "archive_bytes": r.archive_bytes,
        }
        for r in records
    ]
    if _is_json():
        _emit(payload)
        return
    table = Table(box=box.SIMPLE_HEAVY, title="[bold]Backups[/bold]", title_justify="left")
    table.add_column("ID", style="bold")
    table.add_column("Created", style="dim")
    table.add_column("DB bytes", justify="right")
    table.add_column("Archive bytes", justify="right")
    for row in payload:
        table.add_row(
            row["backup_id"],
            row["created_at"],
            str(row["db_bytes"]),
            str(row["archive_bytes"]),
        )
    Console().print(table)


@backup_app.command("restore")
def backup_restore(
    backup_id: str = typer.Argument(..., help="Backup id from `backup list`"),
    confirm: bool = typer.Option(False, "--confirm", help="Required to restore"),
    force_live: bool = typer.Option(
        False, "--force-live", help="Restore even if a live server is running"
    ),
) -> None:
    """Restore a backup over the live data paths (requires --confirm)."""
    from agent_system.services.backup import BackupError

    try:
        dest = _service().restore(backup_id, confirm=confirm, force_live=force_live)
    except BackupError as exc:
        typer.secho(f"restore refused: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from exc
    if _is_json():
        _emit({"restored": backup_id, "db": str(dest)})
        return
    typer.secho(f"✓ restored {backup_id} -> {dest}", fg="green")
