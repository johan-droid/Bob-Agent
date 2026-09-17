"""agentctl chat — interactive REPL with slash commands.

One-liner (backend is already installed)::

    uv run agentctl chat          # REPL
    uv run agentctl chat "goal"   # one-shot (unchanged legacy behavior)
    make chat                     # same REPL via Make

Every message you type becomes a goal session (``POST /api/v1/sessions`` —
the supervisor decomposes it server-side); the REPL then live-tails the
session's event stream and summarizes task outcomes. Slash commands manage
sessions, tasks, approvals, providers, models, and skills inline:

    /help                 this list
    /new                  drop the active session (next message starts fresh)
    /sessions             recent goal sessions
    /attach <id>          switch to a session
    /tasks                tasks of the active session
    /events [n]           recent events of the active session
    /approvals            pending approvals
    /approve <id>         approve (add --deny to deny)

  Live configuration (no restart needed — writes .env.local):

    /providers            LLM providers + routing defaults
    /model                show default provider/model
    /model set <p> [m]    switch default provider/model live
    /model test           ping the default provider
    /skills [on|off <n>]  list skills, or enable/disable one
    /soul                 show the agent's identity (SOUL.md)
    /tools                tool config: shell mode, MCP servers, connectors
    /memory <fact>        remember a durable fact (Obsidian vault)
    /recall <query>       search the vault for relevant notes
    /schedule [rm <id>]   list (or remove) scheduled jobs
    /settings <cmd>       list/get/set/unset/check config keys
    /status               backend health + readiness
    /doctor               probe this machine (no backend needed)
    /setup                re-run the setup wizard inline
    /clear                clear the screen
    /exit                 leave the chat
"""

from __future__ import annotations

import os
import pathlib
import time
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console

from agent_system.cli.main import ApiError, api_request, output, run_command

console = Console()

HISTORY_FILE = ".config/bob-agent/chat_history"

#: Poll cadence while tailing a session's event stream.
POLL_INTERVAL_S = 0.75
#: Stop tailing after this many consecutive empty polls (session went quiet).
QUIET_POLLS_TO_STOP = 8
#: Hard cap on a single tail (seconds).
TAIL_TIMEOUT_S = 180.0


@dataclass
class ChatState:
    """Mutable REPL state (active session + event cursor)."""

    session_id: str | None = None
    session_goal: str = ""
    cursor: int = 0
    running: bool = True
    history: list[str] = field(default_factory=list)
    pending_tokens: bool = False  # a model.token line is mid-render


# ---------------------------------------------------------------------------
# Auth — make `make chat` just work
# ---------------------------------------------------------------------------


def ensure_token() -> bool:
    """Resolve an API token: --token > AGENTCTL_TOKEN > mint via session secret."""
    from agent_system.cli.main import _state

    if _state.get("token"):
        return True
    env_token = os.environ.get("AGENTCTL_TOKEN")
    if env_token:
        _state["token"] = env_token
        return True
    secret = os.environ.get("AGENT_BOOTSTRAP_SECRET") or os.environ.get("API_SESSION_SECRET")
    if not secret:
        from agent_system.cli.setup import _defaults

        saved = _defaults()
        secret = saved.get("AGENT_BOOTSTRAP_SECRET", "") or saved.get("API_SESSION_SECRET", "")
    if not secret:
        return False
    try:
        data = api_request("POST", "/api/v1/auth/token", json={"session_secret": secret})
    except ApiError:
        return False
    token = data.get("token") if isinstance(data, dict) else None
    if not token:
        return False
    _state["token"] = token
    return True


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _short_session(session_id: str) -> str:
    return session_id[:12]


def _summarize_event(event: dict[str, Any]) -> str:
    """One compact human line per event."""
    etype = str(event.get("type", "?"))
    actor = str(event.get("actor", ""))
    payload = event.get("payload") or {}
    detail = ""
    if isinstance(payload, dict):
        for key in ("title", "goal", "model_id", "requested_action", "name", "error"):
            if payload.get(key):
                detail = str(payload[key])[:100]
                break
    line = f"[dim]{etype}[/dim]"
    if actor:
        line += f" [dim]({actor})[/dim]"
    if detail:
        line += f"  {detail}"
    if etype == "approval.requested":
        line = f"[yellow]⚠ approval requested[/yellow]  {detail}  [dim](use /approve)[/dim]"
    elif etype in ("task.completed", "model.completed"):
        line = f"[green]✓ {etype}[/green]  {detail}"
    elif etype in ("task.failed", "model.failed"):
        line = f"[red]✗ {etype}[/red]  {detail}"
    return line


def _print_tasks(tasks: list[dict[str, Any]]) -> None:
    if not tasks:
        console.print("[dim]no tasks yet.[/dim]")
        return
    for task in tasks:
        state = str(task.get("state", "?"))
        color = {"SUCCEEDED": "green", "FAILED": "red", "RUNNING": "cyan"}.get(state, "dim")
        console.print(
            f"  [{color}]•[/{color}] {task.get('title', task.get('id'))}  [dim]{state}[/dim]"
        )


# ---------------------------------------------------------------------------
# Goal flow: send message -> session -> live tail -> summary
# ---------------------------------------------------------------------------


def send_goal(state: ChatState, text: str, watch: bool = True) -> None:
    """Submit a goal as a new session, plan it, run it, tail its progress.

    Creating a session alone schedules nothing — the goal must be planned
    into a task DAG (POST /sessions/{id}/plan) and the ready tasks explicitly
    run (POST /tasks/{id}/run, in-process when no RQ worker exists).
    """
    try:
        created = api_request("POST", "/api/v1/sessions", json={"goal": text})
    except ApiError as exc:
        console.print(f"[red]could not create session: {exc}[/red]")
        return
    state.session_id = created["id"]
    state.session_goal = text
    state.cursor = 0
    console.print(f"[dim]session {_short_session(created['id'])} started.[/dim]")
    try:
        planned = api_request("POST", f"/api/v1/sessions/{state.session_id}/plan")
        task_count = len(planned.get("tasks", [])) if isinstance(planned, dict) else 0
        console.print(f"[dim]planned {task_count} task(s).[/dim]")
    except ApiError as exc:
        console.print(f"[red]planning failed: {exc}[/red]")
        if watch:
            tail_session(state)
        return
    try:
        tasks = api_request("GET", "/api/v1/tasks", params={"session_id": state.session_id})
    except ApiError as exc:
        console.print(f"[red]could not fetch tasks: {exc}[/red]")
        if watch:
            tail_session(state)
        return
    kicked = 0
    for task in tasks or []:
        if task.get("state") in ("PENDING", "QUEUED", "FAILED"):
            try:
                api_request("POST", f"/api/v1/tasks/{task['id']}/run")
                kicked += 1
            except ApiError as exc:
                console.print(f"[yellow]could not run task {task['id']}: {exc}[/yellow]")
    if kicked:
        console.print(f"[dim]running {kicked} task(s)…[/dim]")
    if watch:
        tail_session(state)


def tail_session(state: ChatState, timeout: float = TAIL_TIMEOUT_S) -> None:
    """Poll the active session's events until it goes quiet (Ctrl+C stops)."""
    if not state.session_id:
        console.print("[yellow]no active session — type a message first.[/yellow]")
        return
    quiet = 0
    deadline = time.monotonic() + timeout
    try:
        while state.running and time.monotonic() < deadline and quiet < QUIET_POLLS_TO_STOP:
            try:
                events = api_request(
                    "GET",
                    "/api/v1/events",
                    params={
                        "session_id": state.session_id,
                        "after_sequence": state.cursor,
                        "limit": 100,
                    },
                )
            except ApiError as exc:
                console.print(f"[red]event poll failed: {exc}[/red]")
                return
            if events:
                quiet = 0
                for event in events:
                    state.cursor = max(state.cursor, int(event.get("sequence", 0)))
                    if str(event.get("type")) == "model.token":
                        # Live token rendering: accumulate deltas inline; the
                        # closing model.completed ends the line. Providers
                        # without streaming only ever send model.completed,
                        # so this degrades gracefully to the old display.
                        delta = str((event.get("payload") or {}).get("delta", ""))
                        console.print(delta, end="")
                        state.pending_tokens = True
                        continue
                    if state.pending_tokens:
                        console.print()
                        state.pending_tokens = False
                    console.print(f"  {_summarize_event(event)}")
            else:
                quiet += 1
            if quiet < QUIET_POLLS_TO_STOP:
                time.sleep(POLL_INTERVAL_S)
    except KeyboardInterrupt:
        console.print("\n[dim]tail stopped (session keeps running).[/dim]")
        return
    summarize_session(state)


def summarize_session(state: ChatState) -> None:
    """Print task outcomes for the active session."""
    if not state.session_id:
        return
    try:
        tasks = api_request("GET", "/api/v1/tasks", params={"session_id": state.session_id})
    except ApiError as exc:
        console.print(f"[red]could not fetch tasks: {exc}[/red]")
        return
    done = sum(1 for t in tasks if t.get("state") == "SUCCEEDED")
    failed = sum(1 for t in tasks if t.get("state") == "FAILED")
    console.print(f"[bold]done:[/bold] {done} succeeded, {failed} failed, {len(tasks)} total")
    _print_tasks(tasks)


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------


def cmd_help(state: ChatState, arg: str) -> None:
    """Show slash-command help."""
    console.print(__doc__ or "")


def cmd_new(state: ChatState, arg: str) -> None:
    """Drop the active session."""
    state.session_id = None
    state.session_goal = ""
    state.cursor = 0
    console.print("[dim]fresh start — your next message opens a new session.[/dim]")


def cmd_sessions(state: ChatState, arg: str) -> None:
    """List recent sessions."""
    try:
        sessions = api_request("GET", "/api/v1/sessions", params={"limit": 10})
    except ApiError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    for ses in sessions:
        mark = "→" if ses.get("id") == state.session_id else " "
        console.print(
            f"  {mark} {_short_session(ses['id'])}  [dim]{ses.get('status', '')}[/dim]  "
            f"{str(ses.get('goal', ''))[:70]}"
        )


def cmd_attach(state: ChatState, arg: str) -> None:
    """Switch the active session to <id> (prefix match allowed)."""
    if not arg:
        console.print("[yellow]usage: /attach <session-id>[/yellow]")
        return
    try:
        sessions = api_request("GET", "/api/v1/sessions", params={"limit": 50})
    except ApiError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    match = next((s for s in sessions if s["id"].startswith(arg)), None)
    if match is None:
        console.print(f"[red]no session starting with '{arg}'[/red]")
        return
    state.session_id = match["id"]
    state.session_goal = match.get("goal", "")
    state.cursor = 0
    console.print(f"[dim]attached to {_short_session(match['id'])}.[/dim]")


def cmd_tasks(state: ChatState, arg: str) -> None:
    """Show tasks of the active session."""
    if not state.session_id:
        console.print("[yellow]no active session.[/yellow]")
        return
    summarize_session(state)


def cmd_events(state: ChatState, arg: str) -> None:
    """Show recent events of the active session."""
    if not state.session_id:
        console.print("[yellow]no active session.[/yellow]")
        return
    try:
        limit = int(arg) if arg.strip().isdigit() else 15
    except ValueError:
        limit = 15
    try:
        events = api_request(
            "GET",
            "/api/v1/events",
            params={"session_id": state.session_id, "limit": min(limit, 200)},
        )
    except ApiError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    for event in events:
        console.print(f"  {_summarize_event(event)}")


def cmd_approvals(state: ChatState, arg: str) -> None:
    """List pending approvals."""
    try:
        items = api_request("GET", "/api/v1/approvals", params={"pending_only": "true"})
    except ApiError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    if not items:
        console.print("[dim]no pending approvals.[/dim]")
        return
    for item in items:
        console.print(
            f"  {item.get('id')}  [yellow]{item.get('risk', '')}[/yellow]  "
            f"{item.get('requested_action', '')}  [dim]{item.get('scope', '')}[/dim]"
        )


def cmd_approve(state: ChatState, arg: str) -> None:
    """Decide an approval: /approve <id> [--deny]."""
    parts = arg.split()
    if not parts:
        console.print("[yellow]usage: /approve <id> [--deny][/yellow]")
        return
    approve = "--deny" not in parts
    approval_id = parts[0]
    try:
        api_request(
            "POST",
            f"/api/v1/approvals/{approval_id}/decision",
            json={"approve": approve, "policy": "ALLOW_ONCE"},
        )
    except ApiError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    console.print(f"[green]{'approved' if approve else 'denied'} {approval_id}[/green]")


def cmd_providers(state: ChatState, arg: str) -> None:
    """Show providers + routing defaults."""
    try:
        data = api_request("GET", "/api/v1/model-routing/providers")
    except ApiError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    console.print(
        f"[bold]default:[/bold] {data.get('default_provider')} / {data.get('default_model')}"
    )
    for provider in data.get("providers", []):
        mark = "[green]●[/]" if provider.get("configured") else "[dim]○[/]"
        console.print(f"  {mark} {provider['key']}  [dim]{provider.get('default_model', '')}[/dim]")


def cmd_model(state: ChatState, arg: str) -> None:
    """Show or switch the default provider/model — live, no restart."""
    parts = arg.split()
    if parts and parts[0] == "test":
        try:
            result = api_request("POST", "/api/v1/model-routing/test", json={})
        except ApiError as exc:
            console.print(f"[red]{exc}[/red]")
            return
        if result.get("ok"):
            excerpt = str(result.get("output_excerpt", ""))[:80]
            console.print(f"[green]✓ reachable[/green] {excerpt}")
        else:
            console.print(f"[yellow]! test failed:[/yellow] {result.get('error')}")
        return
    if parts and parts[0] == "set" and len(parts) >= 2:
        from agent_system.cli.settings import set_setting

        provider = parts[1]
        model = parts[2] if len(parts) >= 3 else None
        set_setting("DEFAULT_PROVIDER", provider)
        if model:
            set_setting("DEFAULT_MODEL", model)
        else:
            from agent_system.cli.settings import unset_setting

            unset_setting("DEFAULT_MODEL")  # fall back to the provider default
        console.print(
            f"[green]✓ default →[/green] {provider}"
            + (f" / {model}" if model else " / (provider default)")
            + "  [dim](.env.local — picked up on the next task/model call)[/dim]"
        )
        return
    cmd_providers(state, arg)
    console.print("[dim]tip: /model set <provider> [model] to switch · /model test to ping.[/dim]")


def cmd_skills(state: ChatState, arg: str) -> None:
    """List skills; /skills on|off <name> toggles one."""
    parts = arg.split()
    try:
        if len(parts) == 2 and parts[0] in ("on", "off", "enable", "disable"):
            enabled = parts[0] in ("on", "enable")
            data = api_request("PATCH", f"/api/v1/skills/{parts[1]}", json={"enabled": enabled})
            console.print(f"[green]{'enabled' if enabled else 'disabled'} {data['name']}[/green]")
            return
        data = api_request("GET", "/api/v1/skills")
    except ApiError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    for skill in data.get("skills", []):
        mark = "[green]●[/]" if skill.get("enabled") else "[dim]○[/]"
        console.print(f"  {mark} {skill['name']}  [dim]{skill.get('description', '')}[/dim]")


def cmd_status(state: ChatState, arg: str) -> None:
    """Backend health + readiness."""
    try:
        health = api_request("GET", "/api/v1/health")
        ready = api_request("GET", "/api/v1/ready")
    except ApiError as exc:
        console.print(f"[red]backend unreachable: {exc}[/red]")
        return
    console.print(f"health: {health}  ready: {ready}")


def cmd_doctor(state: ChatState, arg: str) -> None:
    """Probe this machine (works offline, no backend needed)."""
    from agent_system.cli import sysdetect

    sysdetect.render_report(sysdetect.detect_environment())


def cmd_setup(state: ChatState, arg: str) -> None:
    """Re-run the setup wizard inline."""
    from agent_system.cli.setup import setup_command

    setup_command()


def cmd_clear(state: ChatState, arg: str) -> None:
    """Clear the screen."""
    console.clear()


def cmd_soul(state: ChatState, arg: str) -> None:
    """Show the agent's identity (SOUL.md)."""
    from rich.markdown import Markdown

    from agent_system.services.soul import load_soul

    path, text = load_soul()
    if path is None or not text:
        console.print("[yellow]no SOUL.md found.[/yellow]")
        return
    console.print(f"[dim]{path}[/dim]\n")
    console.print(Markdown(text))


def cmd_memory(state: ChatState, arg: str) -> None:
    """/memory <fact> — store a durable fact in the Obsidian vault."""
    from agent_system.config import get_settings
    from agent_system.services.memory_hooks import remember_fact

    fact = arg.strip()
    if not fact:
        console.print("[yellow]usage: /memory <fact to remember>[/yellow]")
        return
    try:
        memory_id = remember_fact(get_settings(), fact, tags=["chat"])
        console.print(f"[green]✓ remembered[/green] [dim]{memory_id}[/dim]")
    except Exception as exc:
        console.print(f"[red]memory write failed: {exc}[/red]")


def cmd_recall(state: ChatState, arg: str) -> None:
    """/recall <query> — keyword-search the vault for relevant notes."""
    from agent_system.config import get_settings
    from agent_system.services.memory_hooks import recall_recent

    query = arg.strip()
    if not query:
        console.print("[yellow]usage: /recall <what to look for>[/yellow]")
        return
    notes = recall_recent(get_settings(), query, 5)
    if not notes:
        console.print("[dim]no matching vault notes.[/dim]")
        return
    for note in notes:
        console.print(f"  [bold]{note['title']}[/bold]  [dim]{note['path']}[/dim]")
        snippet = note["snippet"].replace("\n", " ")[:160]
        console.print(f"    [dim]{snippet}[/dim]")


def cmd_tools(state: ChatState, arg: str) -> None:
    """Show live tool configuration: shell policy, MCP servers, connectors."""
    from agent_system.config import get_settings
    from agent_system.services.mcp import all_servers

    settings = get_settings()
    console.print(
        f"  shell: [bold]{settings.tools_shell_mode}[/bold]  "
        f"approval: [bold]{settings.tools_require_approval}[/bold]  "
        f"max_iters: [bold]{settings.tools_max_iters}[/bold]"
    )
    if settings.openconnector_base_url:
        console.print(
            f"  [green]●[/green] openconnector  [dim]{settings.openconnector_base_url}[/dim]"
            "  [dim](actions + implicit MCP server)[/dim]"
        )
    else:
        console.print("  [dim]○ openconnector (set openconnector_base_url to enable)[/dim]")
    try:
        servers = all_servers(settings)
    except Exception:
        servers = []
    if not servers:
        console.print("  [dim]○ no MCP servers (set MCP_SERVERS JSON to enable)[/dim]")
    for server in servers:
        target = server.url or f"{server.command} {' '.join(server.args)}".strip()
        console.print(f"  [green]●[/green] mcp:{server.name}  [dim]{target}[/dim]")
    console.print("[dim]agent tools: openconnector_list/execute · mcp_list · mcp_call[/dim]")


def cmd_schedule(state: ChatState, arg: str) -> None:
    """List scheduled jobs; /schedule rm <id> removes one."""
    parts = arg.split()
    if parts and parts[0] in ("rm", "delete", "remove") and len(parts) >= 2:
        api_request("DELETE", f"/api/v1/schedule/{parts[1]}")
        console.print(f"[green]✓ removed {parts[1]}[/green]")
        return
    jobs = api_request("GET", "/api/v1/schedule")
    if not jobs:
        console.print("[dim]no scheduled jobs.[/dim]")
        return
    for job in jobs:
        console.print(
            f"  {job.get('id')}  [bold]{job.get('goal', '')[:60]}[/bold]  "
            f"[dim]{job.get('schedule', '')}[/dim]"
        )
    console.print("[dim]create from the dashboard (Schedule tab) or POST /api/v1/schedule.[/dim]")


def cmd_settings(state: ChatState, arg: str) -> None:
    """Manage config inline: list [group] | get KEY | set KEY VALUE | unset KEY | check."""
    from agent_system.cli.settings import (
        SettingsError,
        check_settings,
        get_setting,
        list_settings,
        set_setting,
        unset_setting,
    )

    parts = arg.split()
    if not parts:
        console.print("[yellow]usage: /settings list [group] | get KEY | set KEY VALUE[/yellow]")
        return
    sub, rest = parts[0], parts[1:]
    try:
        if sub == "list":
            rows = list_settings(group=rest[0] if rest else None)
            current_group = ""
            for row in rows:
                if row["group"] != current_group:
                    current_group = row["group"]
                    console.print(f"[cyan]{current_group}[/cyan]")
                console.print(
                    f"  [bold]{row['key']}[/bold] = {row['value']}  [dim]({row['source']})[/dim]"
                )
        elif sub == "get" and rest:
            row = get_setting(rest[0])
            console.print(
                f"[bold]{row['key']}[/bold] = {row['value']}  [dim]({row['source']})[/dim]"
            )
            if row["help"]:
                console.print(f"[dim]{row['help']}[/dim]")
        elif sub == "set" and len(rest) >= 2:
            row = set_setting(rest[0], " ".join(rest[1:]))
            console.print(
                f"[green]✓ {row['key']}[/green] = {row['value']}  [dim](.env.local)[/dim]"
            )
        elif sub == "unset" and rest:
            result = unset_setting(rest[0])
            console.print(f"[green]✓ {result['unset']}[/green] removed from .env.local")
        elif sub == "check":
            report = check_settings()
            if report.get("ok"):
                console.print(
                    f"[green]✓ configuration loads[/green] (default: {report['default_provider']})"
                )
            else:
                console.print(f"[red]invalid: {report.get('error')}[/red]")
        else:
            console.print(
                "[yellow]usage: /settings list [group] | get KEY | set KEY VALUE[/yellow]"
            )
    except SettingsError as exc:
        console.print(f"[red]{exc}[/red]")


def cmd_exit(state: ChatState, arg: str) -> None:
    """Leave the chat."""
    state.running = False
    console.print("[dim]bye.[/dim]")


COMMANDS: dict[str, Any] = {
    "/help": cmd_help,
    "/new": cmd_new,
    "/sessions": cmd_sessions,
    "/attach": cmd_attach,
    "/tasks": cmd_tasks,
    "/events": cmd_events,
    "/approvals": cmd_approvals,
    "/approve": cmd_approve,
    "/providers": cmd_providers,
    "/model": cmd_model,
    "/skills": cmd_skills,
    "/soul": cmd_soul,
    "/memory": cmd_memory,
    "/recall": cmd_recall,
    "/tools": cmd_tools,
    "/mcp": cmd_tools,
    "/schedule": cmd_schedule,
    "/settings": cmd_settings,
    "/status": cmd_status,
    "/doctor": cmd_doctor,
    "/setup": cmd_setup,
    "/clear": cmd_clear,
    "/exit": cmd_exit,
    "/quit": cmd_exit,
    "/q": cmd_exit,
}


def handle_line(state: ChatState, line: str) -> None:
    """Route one REPL line: slash command or goal message."""
    stripped = line.strip()
    if not stripped:
        return
    if stripped.startswith("/"):
        name, _, arg = stripped.partition(" ")
        handler = COMMANDS.get(name)
        if handler is None:
            console.print(f"[yellow]unknown command {name} — try /help[/yellow]")
            return
        try:
            handler(state, arg.strip())
        except ApiError as exc:
            console.print(f"[red]{exc}[/red]")
        return
    send_goal(state, stripped)


# ---------------------------------------------------------------------------
# REPL entrypoints
# ---------------------------------------------------------------------------


def _enable_history() -> None:
    try:
        import readline
    except ImportError:
        return
    history_path = str(pathlib.Path.home() / HISTORY_FILE)
    try:
        readline.read_history_file(history_path)
    except (FileNotFoundError, OSError):
        pass
    import atexit

    def _save() -> None:
        try:
            pathlib.Path(history_path).parent.mkdir(parents=True, exist_ok=True)
            readline.write_history_file(history_path)
        except OSError:
            pass

    atexit.register(_save)


def _banner() -> None:
    console.print(
        "[bold bright_cyan]Bob Agent[/bold bright_cyan] — type a goal, "
        "[dim]/help for commands, /exit to quit.[/dim]"
    )


def _prompt(state: ChatState) -> str:
    if state.session_id:
        return f"[cyan]bob[{_short_session(state.session_id)}][/cyan]› "
    return "[cyan]bob[/cyan]› "


def run_repl() -> int:
    """Interactive chat loop. Returns a process exit code."""
    if not ensure_token():
        console.print(
            "[yellow]No API token and no local API_SESSION_SECRET found.[/yellow]\n"
            "Pass [bold]--token[/bold], set AGENTCTL_TOKEN, or run [bold]make setup[/bold] first."
        )
        return 3
    _banner()
    _enable_history()
    state = ChatState()
    while state.running:
        try:
            line = console.input(_prompt(state))
        except EOFError:
            console.print("")
            break
        except KeyboardInterrupt:
            console.print("\n[dim](Ctrl+C — /exit to quit)[/dim]")
            continue
        handle_line(state, line)
    return 0


def _one_shot(goal: str, watch: bool = False) -> None:
    """Legacy one-shot: create a session for the goal and print it."""
    created = api_request("POST", "/api/v1/sessions", json={"goal": goal})
    if watch:
        state = ChatState(session_id=created["id"], session_goal=goal, cursor=0, running=True)
        try:
            api_request("POST", f"/api/v1/sessions/{created['id']}/plan")
            tasks = api_request("GET", "/api/v1/tasks", params={"session_id": created["id"]})
            for task in tasks or []:
                if task.get("state") in ("PENDING", "QUEUED", "FAILED"):
                    try:
                        api_request("POST", f"/api/v1/tasks/{task['id']}/run")
                    except ApiError:
                        pass
        except ApiError:
            pass
        tail_session(state)
    else:
        output(created)


def run_one_shot(goal: str, watch: bool = False) -> Any:
    """One-shot wrapper with stable exit codes."""
    return run_command(lambda: _one_shot(goal, watch))()
