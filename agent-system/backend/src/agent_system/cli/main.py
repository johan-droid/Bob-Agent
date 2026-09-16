"""agentctl — CLI client (v3.1 Phase 10, §17 CLI spec, §34 CLI Rule).

Typer + Rich CLI that talks to the same `/api/v1` contracts as the web
dashboard — no CLI-only business logic. `--json` gives a stable machine
schema; documented exit codes for scripting:

  0  success
  2  usage error (bad arguments)
  3  authentication failure (401/403)
  4  resource not found (404)
  5  conflict / rejected by backend (409)
  6  backend unavailable (connection refused, 5xx)
"""

from __future__ import annotations

import json as _json
import os
import pathlib
import shutil
import subprocess
import sys
from typing import Any

import httpx
import typer

app = typer.Typer(
    name="agentctl",
    help="Agent System control CLI — same /api/v1 contracts as the dashboard.",
    no_args_is_help=True,
    add_completion=False,
)
workspace_app = typer.Typer(help="Manage coding workspaces", no_args_is_help=True)
tasks_app = typer.Typer(help="Manage tasks", no_args_is_help=True)
approvals_app = typer.Typer(help="Manage pending approvals", no_args_is_help=True)
sessions_app = typer.Typer(help="Manage sessions", no_args_is_help=True)
events_app = typer.Typer(help="Inspect the canonical event stream", no_args_is_help=True)
app.add_typer(workspace_app, name="workspace")
app.add_typer(tasks_app, name="tasks")
app.add_typer(approvals_app, name="approvals")
app.add_typer(sessions_app, name="sessions")
app.add_typer(events_app, name="events")

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_NOT_FOUND = 4
EXIT_CONFLICT = 5
EXIT_UNAVAILABLE = 6

_state: dict[str, Any] = {"base_url": None, "token": None, "json": False, "no_color": False}


class ApiError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code


def client() -> httpx.Client:
    base = _state["base_url"] or "http://127.0.0.1:8000"
    headers: dict[str, str] = {}
    token = _state["token"]
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=base, headers=headers, timeout=30.0)


def api_request(method: str, path: str, **kwargs: Any) -> Any:
    """Single API access point — every command goes through here (v3.1 §34)."""
    try:
        with client() as http:
            resp = http.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise ApiError(EXIT_UNAVAILABLE, f"backend unavailable: {exc}") from exc
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise ApiError(resp.status_code, str(detail))
    if resp.status_code == 204 or not resp.content:
        return None
    return resp.json()


def output(payload: Any) -> None:
    if _state["json"]:
        typer.echo(_json.dumps(payload, indent=2, default=str))
    else:
        if isinstance(payload, list):
            for item in payload:
                typer.echo(str(item))
        else:
            typer.echo(str(payload))


def run_command(fn: Any) -> Any:
    """Decorator: catches ApiError and maps to stable exit codes (§34)."""

    import functools

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            code = fn(*args, **kwargs)
        except ApiError as exc:
            status = exc.status_code
            # api_request may raise with an already-mapped exit code
            # (e.g. EXIT_UNAVAILABLE for connection refused).
            if status in (EXIT_AUTH, EXIT_NOT_FOUND, EXIT_CONFLICT, EXIT_UNAVAILABLE):
                typer.secho(f"error: {exc}", fg="red", err=True)
                sys.exit(status)
            if status in (401, 403):
                typer.secho(f"auth error: {exc}", fg="red", err=True)
                sys.exit(EXIT_AUTH)
            if status == 404:
                typer.secho(f"not found: {exc}", fg="red", err=True)
                sys.exit(EXIT_NOT_FOUND)
            if status == 409:
                typer.secho(f"conflict: {exc}", fg="red", err=True)
                sys.exit(EXIT_CONFLICT)
            if status >= 500:
                typer.secho(f"backend error: {exc}", fg="red", err=True)
                sys.exit(EXIT_UNAVAILABLE)
            typer.secho(f"error: {exc}", fg="red", err=True)
            sys.exit(EXIT_CONFLICT)
        if code is None:
            sys.exit(EXIT_OK)
        sys.exit(code)
        return None

    return wrapper


@app.callback()
def main(
    json_output: bool = typer.Option(False, "--json", help="Machine-readable JSON output"),
    no_color: bool = typer.Option(False, "--no-color", help="Disable colored output"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    api_url: str | None = typer.Option(None, "--api-url", help="Backend base URL"),
    token: str | None = typer.Option(None, "--token", help="Auth token"),
) -> None:
    _state["json"] = json_output
    _state["no_color"] = no_color
    _state["base_url"] = api_url
    _state["token"] = token
    if verbose:
        _state["verbose"] = True


# ---------------------------------------------------------------------------
# version / status / config
# ---------------------------------------------------------------------------


@app.command()
@run_command
def version() -> Any:
    """Show version info."""
    output({"tool": "agentctl", "version": "0.1.0", "api": "v1"})


@app.command()
@run_command
def status() -> Any:
    """Display system status (health + ready)."""
    health = api_request("GET", "/api/v1/health")
    ready = api_request("GET", "/api/v1/ready")
    output({"health": health, "ready": ready})


@app.command()
@run_command
def config() -> Any:
    """Show effective CLI configuration."""
    output(
        {
            "api_url": _state["base_url"] or "http://127.0.0.1:8000",
            "json": _state["json"],
            "no_color": _state["no_color"],
        }
    )


@app.command("setup")
def setup(
    yes: bool = typer.Option(False, "--yes", help="Non-interactive: reuse profile/defaults"),
    section: str | None = typer.Option(None, "--section", help="Run one section only"),
    skip_test: bool = typer.Option(False, "--skip-test", help="Skip provider connectivity test"),
    profile: str | None = typer.Option(None, "--profile", help="Profile file path"),
) -> None:
    """Interactive credential/config wizard → writes .env.local."""
    from agent_system.cli.setup import setup_command

    setup_command(yes=yes, section=section, skip_test=skip_test, profile=profile)


@app.command("doctor")
@run_command
def doctor() -> Any:
    """Probe the machine (OS, Python, uv/git/docker/redis, ports) and report."""
    from agent_system.cli import sysdetect

    report = sysdetect.detect_environment()
    if _state["json"]:
        output(report.to_dict())
    else:
        sysdetect.render_report(report)
    return None


# ---------------------------------------------------------------------------
# sessions / chat
# ---------------------------------------------------------------------------


@app.command("chat")
def chat(
    goal: str | None = typer.Argument(None, help="Goal to submit (omit for REPL)"),
    interactive: bool = typer.Option(False, "--interactive", "-i", help="Force REPL mode"),
    watch: bool = typer.Option(False, "--watch", help="One-shot: tail progress after submit"),
) -> Any:
    """Submit a goal, or open the interactive chat REPL (no goal given)."""
    from agent_system.cli.chat import run_one_shot, run_repl

    if goal is None or interactive:
        code = run_repl()
        sys.exit(code)
        return None
    run_one_shot(goal, watch=watch)
    return None


@sessions_app.command("create")
@run_command
def session_create(goal: str = typer.Argument(...)) -> Any:
    created = api_request("POST", "/api/v1/sessions", json={"goal": goal})
    output(created)


@sessions_app.command("list")
@run_command
def session_list() -> Any:
    output(api_request("GET", "/api/v1/sessions"))


@sessions_app.command("get")
@run_command
def session_get(session_id: str = typer.Argument(...)) -> Any:
    output(api_request("GET", f"/api/v1/sessions/{session_id}"))


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------


@tasks_app.command("list")
@run_command
def tasks_list(
    session_id: str | None = typer.Option(None, "--session"),
    state: str | None = typer.Option(None, "--state"),
) -> Any:
    params: dict[str, Any] = {}
    if session_id:
        params["session_id"] = session_id
    if state:
        params["state"] = state
    output(api_request("GET", "/api/v1/tasks", params=params))


@tasks_app.command("get")
@run_command
def tasks_get(task_id: str = typer.Argument(...)) -> Any:
    output(api_request("GET", f"/api/v1/tasks/{task_id}"))


@tasks_app.command("retry")
@run_command
def tasks_retry(task_id: str = typer.Argument(...)) -> Any:
    output(api_request("POST", f"/api/v1/tasks/{task_id}/retry"))


@tasks_app.command("transition")
@run_command
def tasks_transition(
    task_id: str = typer.Argument(...),
    target: str = typer.Argument(...),
    reason: str | None = typer.Option(None),
) -> Any:
    output(
        api_request(
            "POST", f"/api/v1/tasks/{task_id}/transition", json={"target": target, "reason": reason}
        )
    )


# ---------------------------------------------------------------------------
# approvals
# ---------------------------------------------------------------------------


@approvals_app.command("list")
@run_command
def approvals_list(
    all: bool = typer.Option(False, "--all", help="Include decided records"),
) -> Any:
    params = {"pending_only": "false" if all else "true"}
    output(api_request("GET", "/api/v1/approvals", params=params))


@approvals_app.command("request")
@run_command
def approvals_request(
    action: str = typer.Argument(...),
    risk: str = typer.Argument(...),
    scope: str = typer.Argument(...),
    requester: str = typer.Option("cli", "--requester"),
) -> Any:
    output(
        api_request(
            "POST",
            "/api/v1/approvals",
            json={
                "requested_action": action,
                "risk": risk,
                "scope": scope,
                "requester": requester,
            },
        )
    )


@approvals_app.command("decide")
@run_command
def approvals_decide(
    approval_id: str = typer.Argument(...),
    approve: bool = typer.Option(True, "--approve/--deny"),
    policy: str = typer.Option("ALLOW_ONCE", "--policy"),
) -> Any:
    output(
        api_request(
            "POST",
            f"/api/v1/approvals/{approval_id}/decision",
            json={"approve": approve, "policy": policy},
        )
    )


# ---------------------------------------------------------------------------
# workspaces
# ---------------------------------------------------------------------------


@workspace_app.command("list")
@run_command
def workspace_list() -> Any:
    output(api_request("GET", "/api/v1/workspaces"))


@workspace_app.command("create")
@run_command
def workspace_create(name: str = typer.Argument(...)) -> Any:
    output(api_request("POST", "/api/v1/workspaces", json={"name": name}))


@workspace_app.command("tree")
@run_command
def workspace_tree(workspace_id: str = typer.Argument(...)) -> Any:
    output(api_request("GET", f"/api/v1/workspaces/{workspace_id}/tree"))


@workspace_app.command("delete")
@run_command
def workspace_delete(workspace_id: str = typer.Argument(...)) -> Any:
    api_request("DELETE", f"/api/v1/workspaces/{workspace_id}")
    output({"deleted": workspace_id})


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


@events_app.command("list")
@run_command
def events_list(
    after_sequence: int = typer.Option(0, "--after-sequence"),
    type: str | None = typer.Option(None, "--type"),
    limit: int = typer.Option(200, "--limit"),
) -> Any:
    params: dict[str, Any] = {"after_sequence": after_sequence, "limit": limit}
    if type:
        params["type"] = type
    output(api_request("GET", "/api/v1/events", params=params))


# ---------------------------------------------------------------------------
# web dashboard
# ---------------------------------------------------------------------------

_WEB_DIR = pathlib.Path(__file__).resolve().parents[4] / "web"
_WEB_DEPS_MARKER = _WEB_DIR / "node_modules"


def _web_deps_installed() -> bool:
    """Check if Node.js dependencies for the web dashboard are installed."""
    return _WEB_DEPS_MARKER.is_dir() and any(_WEB_DEPS_MARKER.iterdir())


@app.command("web")
def web_command(
    port: int = typer.Option(3000, "--port", "-p", help="Port for the Web dashboard"),
    production: bool = typer.Option(
        False, "--production", help="Use production build instead of dev server"
    ),
) -> None:
    """Start the Web dashboard.

    Launches the Next.js server so you can interact with the agent through a
    browser UI. Requires Node.js dependencies to be installed (run `npm install`
    in the `web/` directory first).
    """
    if not _WEB_DIR.exists():
        typer.echo(f"[red]Web dashboard directory not found: {_WEB_DIR}[/red]")
        raise typer.Exit(code=EXIT_NOT_FOUND)

    if not _web_deps_installed():
        typer.echo("[yellow]Web dependencies not installed.[/yellow]")
        typer.echo(f"  Run: [bold]cd {_WEB_DIR} && npm install[/bold]")
        raise typer.Exit(code=EXIT_USAGE)

    npm_cmd = shutil.which("npm")
    if npm_cmd is None:
        typer.echo("[red]npm not found. Install Node.js to use the Web dashboard.[/red]")
        raise typer.Exit(code=EXIT_UNAVAILABLE)

    script = "start" if production else "dev"
    typer.echo(f"[bold]Starting Web dashboard on http://localhost:{port}[/bold]")
    typer.echo("  [dim](Press Ctrl+C to stop)[/dim]")

    env = {**os.environ, "PORT": str(port)}

    try:
        subprocess.run([npm_cmd, "run", script], cwd=str(_WEB_DIR), env=env, check=True)
    except subprocess.CalledProcessError as exc:
        typer.echo(f"[red]Web dashboard exited with code {exc.returncode}[/red]")
        raise typer.Exit(code=EXIT_UNAVAILABLE) from exc
    except KeyboardInterrupt as exc:
        typer.echo("\n[yellow]Web dashboard stopped.[/yellow]")
        raise typer.Exit(code=EXIT_OK) from exc


def entrypoint() -> None:
    app()


# Sub-command groups living in their own modules. Imported last so the names
# above (api_request/output/run_command) already exist — avoids a cycle.
from agent_system.cli.backup import backup_app  # noqa: E402
from agent_system.cli.settings import settings_app  # noqa: E402
from agent_system.cli.skills import skills_app  # noqa: E402
from agent_system.cli.soul import soul_app  # noqa: E402
from agent_system.cli.tools import tools_app  # noqa: E402

app.add_typer(backup_app, name="backup")
app.add_typer(tools_app, name="tools")
app.add_typer(skills_app, name="skills")
app.add_typer(soul_app, name="soul")
app.add_typer(settings_app, name="settings")


if __name__ == "__main__":
    entrypoint()
