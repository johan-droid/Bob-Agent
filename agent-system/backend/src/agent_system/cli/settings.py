"""agentctl settings — every key and credential from the CLI.

The full first-run experience (Hermes/OpenClaw-style): ``settings wizard``
walks every configuration value grouped by area; ``list/get/set/unset``
manage individual keys; ``check`` validates the result. Everything persists
to ``.env.local`` (gitignored, overrides ``.env``).

The catalog is derived from the pydantic ``Settings`` model itself, so it
can never drift out of date — every field is manageable, including all
provider keys, secrets, storage paths, and resource limits.
"""

from __future__ import annotations

import json as _json
import pathlib
from dataclasses import dataclass
from typing import Any

import typer
from rich import box
from rich.console import Console
from rich.table import Table

from agent_system.services.providers import PROVIDERS

_console = Console()

GROUP_ORDER = (
    "core",
    "providers",
    "routing",
    "auth",
    "telegram",
    "storage",
    "tools",
    "memory",
    "integrations",
    "scheduler",
    "cost",
    "limits",
    "observability",
)

_GROUPS: dict[str, str] = {
    "agent_env": "core",
    "api_port": "core",
    "api_session_secret": "auth",
    "agent_bootstrap_secret": "auth",
    "telegram_bot_token": "telegram",
    "telegram_allowed_chat_ids": "telegram",
    "telegram_webhook_secret": "telegram",
    "database_url": "storage",
    "vault_path": "storage",
    "workspaces_dir": "storage",
    "templates_dir": "storage",
    "recordings_dir": "storage",
    "outputs_dir": "storage",
    "skills_dir": "storage",
    "soul_path": "storage",
    "default_provider": "routing",
    "default_model": "routing",
    "provider_extra_headers": "routing",
    "daily_budget_usd": "cost",
    "tools_shell_mode": "tools",
    "tools_require_approval": "tools",
    "tools_max_iters": "tools",
    "tools_fs_roots": "tools",
    "tools_plugin_dir": "tools",
    "memory_auto_remember": "memory",
    "memory_recall_top_k": "memory",
    "memory_embedding_provider": "memory",
    "max_context_tokens": "limits",
    "context_compaction_threshold_pct": "limits",
    "circuit_breaker_threshold": "routing",
    "circuit_breaker_cooldown_seconds": "routing",
    "openconnector_base_url": "integrations",
    "openconnector_api_key": "integrations",
    "openconnector_runtime_token": "integrations",
    "openconnector_admin_token": "integrations",
    "openconnector_alias": "integrations",
    "openconnector_user_id": "integrations",
    "mcp_servers": "integrations",
    "a2a_enabled": "integrations",
    "scheduler_enabled": "scheduler",
    "scheduler_timezone": "scheduler",
    "backups_dir": "storage",
    "backup_retention_count": "scheduler",
    "backup_schedule_cron": "scheduler",
    "otel_exporter_otlp_endpoint": "observability",
}

_HELP: dict[str, str] = {
    "agent_env": "Environment name (dev/prod).",
    "api_port": "Backend HTTP port.",
    "api_session_secret": "Signs API tokens (minted via POST /api/v1/auth/token).",
    "agent_bootstrap_secret": "Bootstrap secret accepted by POST /api/v1/auth/token.",
    "anthropic_api_key": "Anthropic key (claude-sonnet-4-5).",
    "openai_api_key": "OpenAI key (gpt-4o-mini).",
    "groq_api_key": "Groq key — generous free tier (gpt-oss-20b).",
    "groq_base_url": "Groq OpenAI-compatible endpoint.",
    "ollama_base_url": "Local Ollama endpoint (keyless).",
    "openrouter_api_key": "OpenRouter key — many free :free models.",
    "openrouter_base_url": "OpenRouter endpoint.",
    "openrouter_site_url": "Site URL sent as OpenRouter HTTP-Referer.",
    "openrouter_app_name": "App name sent as OpenRouter X-Title.",
    "together_api_key": "Together AI key — free-tier turbo models.",
    "together_base_url": "Together AI endpoint.",
    "mistral_api_key": "Mistral key — free open models.",
    "mistral_base_url": "Mistral endpoint.",
    "gemini_api_key": "Google AI Studio key — free Gemini flash.",
    "gemini_base_url": "Gemini endpoint (native API).",
    "deepseek_api_key": "DeepSeek key (deepseek-chat).",
    "deepseek_base_url": "DeepSeek endpoint.",
    "huggingface_api_key": "HuggingFace token — serverless inference.",
    "huggingface_base_url": "HuggingFace router endpoint.",
    "tokenrouter_api_key": "TokenRouter key (multi-provider gateway).",
    "tokenrouter_base_url": "TokenRouter endpoint.",
    "default_provider": "Provider key used when no task rule matches (echo = offline).",
    "default_model": "Default model id (empty = provider default).",
    "provider_extra_headers": "Extra headers for every provider call, as JSON.",
    "telegram_bot_token": "Bot token from @BotFather (empty = gateway off).",
    "telegram_allowed_chat_ids": "Comma-separated chat ids allowed to use the bot.",
    "telegram_webhook_secret": "Set = webhook mode; empty = polling mode.",
    "database_url": "SQLAlchemy URL (SQLite by default).",
    "vault_path": "Obsidian memory vault directory.",
    "workspaces_dir": "Coding workspace roots.",
    "templates_dir": "Workspace template tarballs.",
    "recordings_dir": "Behavior recordings (.jsonl).",
    "outputs_dir": "Generated file outputs.",
    "skills_dir": "Agent skills (SKILL.md folders).",
    "soul_path": "Agent identity file (empty = auto-discover SOUL.md).",
    "daily_budget_usd": "Daily model-spend budget cap.",
    "tools_shell_mode": "Shell policy: sandbox (Docker), local (host opt-in), or off.",
    "tools_require_approval": "Execute tools demand a live approval first.",
    "tools_max_iters": "Max Reason+Act iterations per LLM task.",
    "tools_fs_roots": "Extra comma-separated roots for file tools.",
    "tools_plugin_dir": "Folder-drop tool plugins (same pattern as skills).",
    "memory_auto_remember": "Persist task outcomes to the Obsidian vault.",
    "memory_recall_top_k": "Vault notes injected into LLM task prompts.",
    "memory_embedding_provider": (
        "Embedding backend: hash (offline default) or local "
        "(sentence-transformers, needs memory extra)."
    ),
    "max_context_tokens": "ReAct context budget in estimated tokens (chars/4 heuristic).",
    "context_compaction_threshold_pct": (
        "Compact oldest tool results past this % of the context budget."
    ),
    "circuit_breaker_threshold": "Consecutive provider failures before the circuit opens.",
    "circuit_breaker_cooldown_seconds": "Fast-fail cooldown while a provider circuit is open.",
    "openconnector_base_url": "OpenConnector runtime URL (empty = tools hidden; docker: http://localhost:3000).",
    "openconnector_api_key": "OpenConnector bearer token (alias of openconnector_runtime_token).",
    "openconnector_runtime_token": "Runtime token for /v1 + /mcp calls (optional on localhost).",
    "openconnector_admin_token": "Admin token for action guides + console (optional).",
    "openconnector_alias": "Named connection sent as x-oo-connector-alias (optional).",
    "openconnector_user_id": "Deprecated — unused by the OpenConnector API.",
    "mcp_servers": "MCP stdio servers as JSON list.",
    "a2a_enabled": "Outbound agent delegation (off by default; needs per-target approval).",
    "scheduler_enabled": "Run the cron/interval scheduler in the API server.",
    "scheduler_timezone": "Timezone for cron schedules.",
    "backups_dir": "Where backup runs are stored.",
    "backup_retention_count": "Newest backup runs to keep (older pruned).",
    "backup_schedule_cron": "Backup cadence as cron (default daily 03:00).",
    "otel_exporter_otlp_endpoint": (
        "OTLP collector endpoint (empty = telemetry off, zero overhead)."
    ),
    "max_concurrent_agents": "Max parallel agent runs.",
    "max_concurrent_tasks": "Max parallel tasks.",
    "max_workspace_size_mb": "Max workspace size on disk.",
    "max_file_size_mb": "Max single file read/write.",
    "max_output_size_mb": "Max generated output size.",
    "max_log_size_mb": "Max log capture size.",
    "max_browser_sessions": "Max parallel browser sessions.",
    "max_container_cpu": "Max CPUs per sandbox container.",
    "max_container_memory_mb": "Max RAM per sandbox container.",
    "max_execution_time_seconds": "Max seconds per execution.",
    "max_task_tokens": "Max tokens per task.",
    "max_task_cost_usd": "Max spend per task.",
    "max_retries": "Max retries per task.",
}

_SECRET_HINTS = ("KEY", "SECRET", "TOKEN")


def is_secret_key(env: str) -> bool:
    """True for credential env vars (suffix match — avoids URL false positives)."""
    name = env.strip().upper()
    return name.endswith(("_KEY", "_SECRET", "_TOKEN")) or name in (
        "PROVIDER_EXTRA_HEADERS",
        "MCP_SERVERS",
    )


class SettingsError(ValueError):
    """Raised for unknown keys or invalid values."""


@dataclass(frozen=True)
class SettingSpec:
    field: str
    env: str
    group: str
    secret: bool
    type_name: str
    help: str


def _base_type(annotation: Any) -> Any:
    """Unwrap Optional[X] / X | None to X."""
    import types
    import typing

    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin in (typing.Union, types.UnionType):
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _base_type(non_none[0])
    return annotation


def _type_name(annotation: Any) -> str:
    base = _base_type(annotation)
    if base is bool:
        return "bool"
    if base is int:
        return "int"
    if base is float:
        return "float"
    if base is pathlib.Path:
        return "path"
    return "str"


def catalog() -> list[SettingSpec]:
    """Every manageable setting, derived from the Settings model."""
    from agent_system.config import Settings

    specs: list[SettingSpec] = []
    for field_name, field_info in Settings.model_fields.items():
        env = field_name.upper()
        group = _GROUPS.get(field_name, "limits" if field_name.startswith("max_") else "providers")
        specs.append(
            SettingSpec(
                field=field_name,
                env=env,
                group=group,
                secret=is_secret_key(env),
                type_name=_type_name(field_info.annotation),
                help=_HELP.get(field_name, ""),
            )
        )
    order = {name: i for i, name in enumerate(GROUP_ORDER)}
    specs.sort(key=lambda s: (order.get(s.group, 99), s.env))
    return specs


def _find_spec(key: str) -> SettingSpec:
    wanted = key.strip().upper()
    for spec in catalog():
        if spec.env == wanted or spec.field == key.strip().lower():
            return spec
    valid = ", ".join(s.env for s in catalog())
    raise SettingsError(f"unknown setting '{key}'. Valid keys: {valid}")


def _read_env_file(path: pathlib.Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        value = value.strip()
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        out[key.strip()] = value
    return out


def sources() -> tuple[dict[str, str], dict[str, str]]:
    """Return (.env values, .env.local values)."""
    return _read_env_file(pathlib.Path(".env")), _read_env_file(pathlib.Path(".env.local"))


def effective() -> Any:
    """Current merged Settings (defaults < .env < .env.local < environ)."""
    from agent_system.config import Settings

    return Settings()


def mask(value: str) -> str:
    """Mask a secret for display (shows set-state, not content)."""
    if not value:
        return "(not set)"
    return "•••••••• (set)"


def _display_value(spec: SettingSpec, value: Any, show_secrets: bool = False) -> str:
    text = "" if value is None else str(value)
    if spec.secret and not show_secrets:
        return mask(text)
    return text if text else "(not set)"


def _value_source(env: str, dot_env: dict[str, str], local: dict[str, str]) -> str:
    if env in local:
        return ".env.local"
    if env in dot_env:
        return ".env"
    return "default"


def list_settings(group: str | None = None, show_secrets: bool = False) -> list[dict[str, Any]]:
    """Describe every setting (or one group) with value + source."""
    if group is not None and group not in GROUP_ORDER:
        raise SettingsError(f"unknown group '{group}'. Groups: {', '.join(GROUP_ORDER)}")
    settings = effective()
    dot_env, local = sources()
    rows: list[dict[str, Any]] = []
    for spec in catalog():
        if group is not None and spec.group != group:
            continue
        raw = getattr(settings, spec.field, None)
        rows.append(
            {
                "key": spec.env,
                "value": _display_value(spec, raw, show_secrets),
                "source": _value_source(spec.env, dot_env, local),
                "group": spec.group,
                "type": spec.type_name,
                "secret": spec.secret,
                "help": spec.help,
            }
        )
    return rows


def get_setting(key: str, show_secrets: bool = False) -> dict[str, Any]:
    """Describe one setting."""
    spec = _find_spec(key)
    settings = effective()
    dot_env, local = sources()
    raw = getattr(settings, spec.field, None)
    return {
        "key": spec.env,
        "value": _display_value(spec, raw, show_secrets),
        "source": _value_source(spec.env, dot_env, local),
        "group": spec.group,
        "type": spec.type_name,
        "secret": spec.secret,
        "help": spec.help,
    }


def parse_value(spec: SettingSpec, raw: str) -> Any:
    """Parse + validate a raw string for a setting (raises SettingsError)."""
    text = raw.strip()
    if spec.type_name == "bool":
        lowered = text.lower()
        if lowered in ("1", "true", "yes", "y", "on"):
            return True
        if lowered in ("0", "false", "no", "n", "off"):
            return False
        raise SettingsError(f"{spec.env} must be a bool (true/false), got '{raw}'")
    if spec.type_name == "int":
        try:
            return int(text)
        except ValueError:
            raise SettingsError(f"{spec.env} must be an integer, got '{raw}'") from None
    if spec.type_name == "float":
        try:
            return float(text)
        except ValueError:
            raise SettingsError(f"{spec.env} must be a number, got '{raw}'") from None
    if spec.type_name == "path":
        return str(pathlib.Path(text).expanduser())
    # str + field-specific validation
    if spec.env == "DEFAULT_PROVIDER" and text not in (*PROVIDERS, "echo"):
        raise SettingsError(f"unknown provider '{text}'. Valid: echo, {', '.join(PROVIDERS)}")
    if spec.env.endswith("_BASE_URL") and not text.startswith(("http://", "https://")):
        raise SettingsError(f"{spec.env} must be an http(s) URL, got '{raw}'")
    if spec.env == "PROVIDER_EXTRA_HEADERS":
        try:
            parsed = _json.loads(text or "{}")
        except _json.JSONDecodeError:
            raise SettingsError(f"{spec.env} must be a JSON object, got '{raw}'") from None
        if not isinstance(parsed, dict):
            raise SettingsError(f"{spec.env} must be a JSON object")
        return _json.dumps(parsed)
    return text


def set_setting(key: str, raw_value: str) -> dict[str, Any]:
    """Validate, persist to .env.local, and return the new description."""
    from agent_system.cli.setup import write_env
    from agent_system.config import Settings

    spec = _find_spec(key)
    value = parse_value(spec, raw_value)
    try:
        Settings(**{spec.field: value})
    except Exception as exc:
        raise SettingsError(f"{spec.env} rejected: {exc}") from exc
    write_env({spec.env: str(value)})
    return get_setting(spec.env)


def unset_setting(key: str) -> dict[str, str]:
    """Remove a key from .env.local (falls back to .env/default)."""
    spec = _find_spec(key)
    path = pathlib.Path(".env.local")
    if not path.is_file():
        return {"unset": spec.env, "note": "not present in .env.local"}
    kept: list[str] = []
    removed = False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            name, _, _ = stripped.partition("=")
            if name.strip() == spec.env:
                removed = True
                continue
        kept.append(line)
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return {"unset": spec.env, "removed": str(removed)}


def check_settings() -> dict[str, Any]:
    """Load Settings and report health (never raises)."""
    try:
        settings = effective()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    from agent_system.services.providers import configured_providers
    from agent_system.services.soul import find_soul

    providers = configured_providers(settings)
    active = [p["key"] for p in providers if p["configured"]]
    return {
        "ok": True,
        "default_provider": settings.default_provider,
        "providers_configured": active,
        "soul_path": str(find_soul(settings.soul_path or None) or "(none)"),
        "notes": (
            ["no LLM providers configured — echo (offline) mode"]
            if not active or active == ["echo"]
            else []
        ),
    }


def run_wizard(yes: bool = False) -> dict[str, str]:
    """Exhaustive first-run wizard over every setting. Returns changed values."""
    from agent_system.cli.setup import _ask, _confirm, _masked, write_env

    _console.print(
        "[bold bright_cyan]Bob Agent[/bold bright_cyan] — full settings wizard\n"
        "[dim]Every key and credential, grouped. Enter keeps the current value.[/dim]\n"
    )
    settings = effective()
    dot_env, local = sources()
    changed: dict[str, str] = {}
    current_group = ""
    for spec in catalog():
        if spec.group != current_group:
            current_group = spec.group
            _console.print(f"\n[bold]— {current_group} —[/bold]")
        raw = getattr(settings, spec.field, None)
        current = "" if raw is None else str(raw)
        if spec.help:
            _console.print(f"  [dim]{spec.env}[/dim] [dim]({spec.help})[/dim]")
        if yes:
            continue
        if spec.secret:
            _console.print(f"  [dim]{spec.env} {'(set)' if current else '(not set)'}[/dim]")
            if spec.env in ("API_SESSION_SECRET", "AGENT_BOOTSTRAP_SECRET") and current:
                if _confirm(f"    Regenerate {spec.env}?", default=False):
                    import secrets

                    changed[spec.env] = secrets.token_urlsafe(32)
                continue
            val = _masked("   value (Enter keeps current)")
            if val and val != current:
                changed[spec.env] = val
            continue
        val = _ask(f"  {spec.env}", current)
        if val != current:
            try:
                parsed = parse_value(spec, val)
                changed[spec.env] = str(parsed)
            except SettingsError as exc:
                _console.print(f"  [yellow]skipped: {exc}[/yellow]")
    if not changed:
        _console.print("\n[dim]no changes.[/dim]")
        return {}
    _console.print(f"\n[bold]Changed {len(changed)} setting(s):[/bold]")
    for key in sorted(changed):
        _console.print(
            f"  {key} = {mask(changed[key]) if _find_spec(key).secret else changed[key]}"
        )
    if yes or _confirm("  Write to .env.local?", default=True):
        write_env(changed)
        return changed
    return {}


# ---------------------------------------------------------------------------
# Typer command group (file-local; registered by cli.main, no import cycle)
# ---------------------------------------------------------------------------

settings_app = typer.Typer(
    help="Manage every setting and credential (list/get/set/unset/wizard)",
    no_args_is_help=True,
)


def _emit(payload: Any) -> None:
    """Output honoring the global --json flag."""
    from agent_system.cli.main import output as _output

    _output(payload)


def _is_json() -> bool:
    from agent_system.cli.main import _state

    return bool(_state.get("json"))


@settings_app.command("list")
def settings_list(
    group: str | None = typer.Option(None, "--group", help="Filter to a group"),
    show_secrets: bool = typer.Option(False, "--show-secrets", help="Reveal secret values"),
) -> None:
    """List all settings with values (masked) and sources."""
    try:
        rows = list_settings(group=group, show_secrets=show_secrets)
    except SettingsError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if _is_json():
        _emit(rows)
        return
    table = Table(box=box.SIMPLE_HEAVY, title="[bold]Settings[/bold]", title_justify="left")
    table.add_column("Key", style="bold")
    table.add_column("Value", overflow="fold")
    table.add_column("Source", style="dim")
    table.add_column("Group", style="dim")
    current_group = ""
    for row in rows:
        if row["group"] != current_group:
            current_group = row["group"]
            table.add_row(f"[cyan]{current_group}[/cyan]", "", "", "")
        table.add_row(f"  {row['key']}", row["value"], row["source"], "")
    _console.print(table)
    _console.print("[dim]Sources: .env.local > .env > default. Secrets masked.[/dim]")


@settings_app.command("get")
def settings_get(
    key: str = typer.Argument(..., help="Setting key, e.g. GROQ_API_KEY"),
    show_secrets: bool = typer.Option(False, "--show-secrets", help="Reveal secret values"),
) -> None:
    """Show one setting with its source."""
    try:
        row = get_setting(key, show_secrets=show_secrets)
    except SettingsError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if _is_json():
        _emit(row)
        return
    _console.print(f"[bold]{row['key']}[/bold] = {row['value']}  [dim]({row['source']})[/dim]")
    if row["help"]:
        _console.print(f"[dim]{row['help']}[/dim]")


@settings_app.command("set")
def settings_set(
    key: str = typer.Argument(..., help="Setting key, e.g. DAILY_BUDGET_USD"),
    value: str = typer.Argument(..., help="New value"),
) -> None:
    """Validate and persist a setting to .env.local."""
    try:
        row = set_setting(key, value)
    except SettingsError as exc:
        raise typer.BadParameter(str(exc)) from exc
    _console.print(f"[green]✓ {row['key']}[/green] = {row['value']}  [dim](.env.local)[/dim]")


@settings_app.command("unset")
def settings_unset(key: str = typer.Argument(..., help="Setting key to remove")) -> None:
    """Remove a key from .env.local (falls back to .env/default)."""
    try:
        result = unset_setting(key)
    except SettingsError as exc:
        raise typer.BadParameter(str(exc)) from exc
    _console.print(f"[green]✓ {result['unset']}[/green] removed from .env.local")


@settings_app.command("check")
def settings_check() -> None:
    """Validate the effective configuration and report status."""
    from agent_system.cli.main import output as _output

    report = check_settings()
    if _is_json():
        _output(report)
        return
    if not report.get("ok"):
        _console.print(f"[red]invalid configuration: {report.get('error')}[/red]")
        raise typer.Exit(code=1)
    _console.print(f"[green]✓ configuration loads[/green] (default: {report['default_provider']})")
    active = report.get("providers_configured", [])
    _console.print(f"  providers: {', '.join(active) if active else 'none (echo mode)'}")
    _console.print(f"  soul: {report.get('soul_path')}")
    for note in report.get("notes", []):
        _console.print(f"  [yellow]! {note}[/yellow]")


@settings_app.command("wizard")
def settings_wizard(
    yes: bool = typer.Option(False, "--yes", help="Accept all current values"),
) -> None:
    """Exhaustive first-run wizard over every setting."""
    run_wizard(yes=yes)


@settings_app.command("path")
def settings_path() -> None:
    """Show which env files exist and take precedence."""
    for name in (".env", ".env.local"):
        path = pathlib.Path(name)
        mark = "[green]exists[/]" if path.is_file() else "[dim]missing[/dim]"
        _console.print(f"  {name}  {mark}")
    _console.print("[dim]Precedence: environment > .env.local > .env > defaults.[/dim]")
