"""agentctl setup — interactive credential/config wizard.

Prompts for credentials, provider selection, and settings with rich defaults,
then writes them to `.env.local` (gitignored, higher precedence than `.env`).
Pressing Enter accepts the default for each item; secrets are read via masked
input where the terminal supports it.

The wizard remembers non-secret answers in a profile
(``~/.config/bob-agent/setup.json``) so re-runs offer to reuse them instead
of asking everything again. It also probes the machine first (``[0]``
environment section) and adapts its hints.

Run inside the project root (where `.env` lives):  `uv run agentctl setup`

Non-interactive / partial runs::

    agentctl setup --yes                  # accept profile+defaults, no prompts
    agentctl setup --section telegram     # re-run one section only
    agentctl setup --skip-test            # skip provider connectivity test
    agentctl setup --profile ./team.json  # use a custom profile file
"""

from __future__ import annotations

import json
import os
import pathlib
import secrets

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from agent_system.cli.settings import is_secret_key
from agent_system.services.providers import _KEYLESS_PROVIDERS, PROVIDERS

ENV_LOCAL = ".env.local"

#: Sections the wizard walks through (also valid `--section` values).
SECTIONS = ("env", "providers", "auth", "telegram", "storage", "limits")

_console = Console()

#: provider key -> (api key env var, base url env var)
_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "together": "TOGETHER_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "huggingface": "HUGGINGFACE_API_KEY",
    "freellmapi": "FREELLMAPI_API_KEY",
    "tokenrouter": "TOKENROUTER_API_KEY",
}
_BASE_ENV = {
    "groq": "GROQ_BASE_URL",
    "ollama": "OLLAMA_BASE_URL",
    "openrouter": "OPENROUTER_BASE_URL",
    "together": "TOGETHER_BASE_URL",
    "mistral": "MISTRAL_BASE_URL",
    "gemini": "GEMINI_BASE_URL",
    "deepseek": "DEEPSEEK_BASE_URL",
    "huggingface": "HUGGINGFACE_BASE_URL",
    "freellmapi": "FREELLMAPI_BASE_URL",
    "tokenrouter": "TOKENROUTER_BASE_URL",
}
_ORDER = (
    "openai",
    "anthropic",
    "groq",
    "ollama",
    "openrouter",
    "together",
    "mistral",
    "gemini",
    "deepseek",
    "huggingface",
    "freellmapi",
    "tokenrouter",
)


def _masked(prompt: str, default: str | None = None) -> str:
    """Read a value; echo nothing (or ``****``) while typing for secrets."""
    try:
        from getpass import getpass

        value = getpass(f"{prompt} ")
        return value.strip() or (default or "")
    except Exception:
        return default or ""


def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = typer.prompt(f"{prompt}{suffix}", default="", show_default=False)
    value = value.strip()
    return value or (default or "")


def _confirm(prompt: str, default: bool = True) -> bool:
    return typer.confirm(prompt, default=default)


# ---------------------------------------------------------------------------
# Answer profiles — remember non-secret choices between runs
# ---------------------------------------------------------------------------


def profile_path(explicit: str | None = None) -> pathlib.Path:
    """Resolve which profile file to use (explicit > env > default)."""
    if explicit:
        return pathlib.Path(explicit)
    env = os.environ.get("BOB_PROFILE")
    if env:
        return pathlib.Path(env)
    return pathlib.Path.home() / ".config" / "bob-agent" / "setup.json"


def load_profile(path: pathlib.Path) -> dict[str, str]:
    """Load remembered answers; empty dict when missing/corrupt (never raises)."""
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def save_profile(path: pathlib.Path, values: dict[str, str]) -> None:
    """Persist non-secret answers for the next run (best-effort)."""
    remember = {k: v for k, v in values.items() if not is_secret_key(k)}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(remember, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except Exception:
        pass


def _profile_pick_default(profile: dict[str, str]) -> str:
    """Translate remembered PROVIDERS_PICKED back into picker numbers."""
    picked = [p.strip() for p in profile.get("PROVIDERS_PICKED", "").split(",") if p.strip()]
    numbers = [str(_ORDER.index(p) + 1) for p in picked if p in _ORDER]
    return ",".join(numbers) if numbers else "0"


# ---------------------------------------------------------------------------
# Rich UI building blocks
# ---------------------------------------------------------------------------


def _banner() -> None:
    _console.print(
        Panel.fit(
            "[bold bright_cyan]Bob Agent[/bold bright_cyan] setup  "
            "[dim]· Enter accepts [bright_white]brackets[/bright_white] · "
            "everything stays re-configurable later (/settings, /setup)[/dim]",
            box=box.ROUNDED,
            border_style="bright_cyan",
            padding=(0, 1),
        )
    )


def _section(index: int, title: str) -> None:
    _console.print(
        f"\n[bold bright_cyan]{index}[/bold bright_cyan]·[bold]{title}[/bold] "
        f"[dim]{'─' * max(52 - len(title), 4)}[/dim]"
    )


def _provider_table(defaults: dict[str, str]) -> None:
    """Render the provider catalog with configured state."""
    table = Table(
        box=box.SIMPLE,
        title="[bold]Providers[/bold]  [dim]· pick by number[/dim]",
        title_justify="left",
        pad_edge=False,
    )
    table.add_column("#", justify="right", style="dim", width=2, no_wrap=True)
    table.add_column("Provider", style="bold", no_wrap=True)
    table.add_column("Key", justify="center", width=5)
    table.add_column("Free", justify="center", width=4)
    for i, key in enumerate(_ORDER, start=1):
        spec = PROVIDERS[key]
        key_env = _KEY_ENV.get(key)
        configured = (
            "●" if (key in _KEYLESS_PROVIDERS or (key_env and defaults.get(key_env))) else "○"
        )
        table.add_row(
            str(i),
            spec.label,
            "–" if key in _KEYLESS_PROVIDERS else configured,
            "✓" if spec.free_tier else "–",
        )
    _console.print(table)
    _console.print("[dim]● configured  ○ not configured  – n/a[/dim]")


def _pick_providers(default_pick: str = "0") -> list[str]:
    """Let the user pick provider numbers, or '0' for none."""
    while True:
        raw = _ask(
            "Providers to configure (comma-separated numbers, 0 = none, a = all)",
            default_pick,
        )
        raw = raw.strip().lower().replace(" ", "")
        if raw == "":
            raw = "0"
        picked: list[str] = []
        if raw == "a":
            picked = list(_ORDER)
        elif raw == "0" or raw == "":
            picked = []
        else:
            ok = True
            for token in raw.split(","):
                if not token.isdigit() or not (1 <= int(token) <= len(_ORDER)):
                    ok = False
                    break
                picked.append(_ORDER[int(token) - 1])
            if not ok:
                _console.print("[red]Invalid selection — try again.[/red]")
                continue
        if not picked:
            _console.print(
                "[dim]No providers selected — the system will boot in offline (Echo) mode.[/dim]"
            )
        return picked


def _collect_provider_keys(
    picked: list[str], defaults: dict[str, str], out: dict[str, str]
) -> None:
    """Ask for keys / base URLs / models for each chosen provider."""
    for key in picked:
        spec = PROVIDERS[key]
        _console.print(f"\n[bold cyan]▸ {spec.label}[/bold cyan]  [dim]{spec.description}[/dim]")
        if key not in _KEYLESS_PROVIDERS:
            env_key = _KEY_ENV[key]
            current = defaults.get(env_key)
            label = f"  {env_key}"
            if current:
                label += " (already set — Enter keeps it)"
            val = _masked(label)
            if val:
                out[env_key] = val
            elif current:
                out[env_key] = current
        else:
            _console.print("  [dim](keyless — local server only)[/dim]")
        base_env = _BASE_ENV.get(key)
        if base_env:
            default_base = defaults.get(base_env, spec.base_url)
            val = _ask(f"  {base_env}", default_base)
            if val and val != spec.base_url:
                out[base_env] = val


def _pick_default_provider(
    picked: list[str], out: dict[str, str], defaults: dict[str, str]
) -> None:
    """Choose which configured provider is the default."""
    available = [
        p
        for p in picked
        if p in _KEYLESS_PROVIDERS or _KEY_ENV.get(p) in out or _KEY_ENV.get(p) in defaults
    ]
    if not available:
        out["DEFAULT_PROVIDER"] = defaults.get("DEFAULT_PROVIDER", "echo")
        _console.print(
            "[dim]No providers configured — routing to [bold]echo[/bold] (offline) mode.[/dim]"
        )
        return
    options = "\n".join(f"    [{i}] {PROVIDERS[k].label}" for i, k in enumerate(available, start=1))
    _console.print(f"\n[bold]Default provider[/bold]\n{options}")
    while True:
        choice = _ask("  Choose provider number", "1")
        if choice.isdigit() and 1 <= int(choice) <= len(available):
            selected = available[int(choice) - 1]
            break
        _console.print("[red]Invalid choice — try again.[/red]")
    out["DEFAULT_PROVIDER"] = selected
    model = _ask(
        "  Default model for this provider",
        defaults.get("DEFAULT_MODEL") or PROVIDERS[selected].default_model,
    )
    if model:
        out["DEFAULT_MODEL"] = model


# ---------------------------------------------------------------------------
# Wizard sections (each usable standalone via --section)
# ---------------------------------------------------------------------------


def _section_env(defaults: dict[str, str], out: dict[str, str], yes: bool) -> None:
    """[env] Probe the machine and adapt hints (stores nothing)."""
    _section(0, "Environment")
    from agent_system.cli import sysdetect

    report = sysdetect.detect_environment()
    sysdetect.render_report(report)
    if not report.redis_reachable and not report.tools["docker-compose"].found:
        _console.print(
            "[dim]Tip: start Redis with [bold]make up[/bold] (needs Docker), "
            "or point REDIS_URL at an existing server.[/dim]"
        )
    if not yes:
        _console.print("")


def _section_providers(
    defaults: dict[str, str],
    out: dict[str, str],
    yes: bool,
    skip_test: bool,
    profile: dict[str, str],
) -> None:
    """[providers] Pick providers, collect keys, choose default, test."""
    _section(1, "LLM Providers")
    if yes:
        provider = defaults.get("DEFAULT_PROVIDER", "echo")
        out["DEFAULT_PROVIDER"] = provider
        if defaults.get("DEFAULT_MODEL"):
            out["DEFAULT_MODEL"] = defaults["DEFAULT_MODEL"]
        _console.print(f"[dim]Non-interactive: default provider [bold]{provider}[/bold].[/dim]")
        return
    _provider_table(defaults)
    picked = _pick_providers(_profile_pick_default(profile))
    if picked:
        out["PROVIDERS_PICKED"] = ",".join(picked)
        _collect_provider_keys(picked, defaults, out)
        _pick_default_provider(picked, out, defaults)
    if not skip_test and _confirm("  Test the default provider connectivity now?", default=False):
        from agent_system.config import Settings
        from agent_system.services.providers import test_provider

        settings = Settings()
        provider = out.get("DEFAULT_PROVIDER", "echo")
        model = out.get("DEFAULT_MODEL")
        if provider != "echo":
            result = test_provider(settings, provider, model=model)
            if result["ok"]:
                _console.print(
                    f"[green]✓ {provider} reachable[/green]: "
                    f"{result.get('output_excerpt', '')[:80]}"
                )
            else:
                _console.print(f"[yellow]! {provider} test failed:[/yellow] {result.get('error')}")
    _console.print("")


def _section_auth(defaults: dict[str, str], out: dict[str, str], yes: bool) -> None:
    """[auth] API + bootstrap secrets (kept, never rotated silently)."""
    _section(2, "Authentication")
    for env_key in ("API_SESSION_SECRET", "AGENT_BOOTSTRAP_SECRET"):
        current = defaults.get(env_key)
        if yes:
            out[env_key] = current or secrets.token_urlsafe(32)
            continue
        if current:
            if _confirm(f"  {env_key} already set — regenerate?", default=False):
                out[env_key] = secrets.token_urlsafe(32)
                _console.print(f"    [green]→ regenerated[/green] {out[env_key][:12]}…")
            else:
                out[env_key] = current
        elif _confirm(f"  Generate a random {env_key}?", default=True):
            out[env_key] = secrets.token_urlsafe(32)
            _console.print(f"    [green]→ generated[/green] {out[env_key][:12]}…")
        else:
            val = _masked(f"  {env_key}")
            if val:
                out[env_key] = val
    _console.print("")


def _section_telegram(defaults: dict[str, str], out: dict[str, str], yes: bool) -> None:
    """[telegram] Bot token + chat allowlist (optional)."""
    _section(3, "Telegram Bot (optional)")
    if yes:
        if defaults.get("TELEGRAM_BOT_TOKEN"):
            out["TELEGRAM_BOT_TOKEN"] = defaults["TELEGRAM_BOT_TOKEN"]
            _console.print("[dim]Non-interactive: keeping existing Telegram token.[/dim]")
        return
    bot_token = _masked("  Telegram Bot Token (from @BotFather, optional)")
    if bot_token:
        out["TELEGRAM_BOT_TOKEN"] = bot_token
        chat_ids = _ask("  Allowed Chat IDs (comma-separated, from @userinfobot)")
        if chat_ids:
            out["TELEGRAM_ALLOWED_CHAT_IDS"] = chat_ids
        webhook = _ask("  Webhook Secret (empty = polling mode)")
        if webhook:
            out["TELEGRAM_WEBHOOK_SECRET"] = webhook
    _console.print("")


def _section_storage(defaults: dict[str, str], out: dict[str, str], yes: bool) -> None:
    """[storage] Vault / workspace dirs + provider HTTP headers."""
    _section(4, "Storage & Requests")
    home_vault = str(pathlib.Path.home() / "Downloads" / "Claude memory")
    vault_default = defaults.get("VAULT_PATH", home_vault)
    ws_default = defaults.get("WORKSPACES_DIR", "workspaces")
    if yes:
        out["VAULT_PATH"] = vault_default
        out["WORKSPACES_DIR"] = ws_default
        return
    vault = _ask("  Vault Path", vault_default)
    if vault != defaults.get("VAULT_PATH", ""):
        out["VAULT_PATH"] = vault
    ws = _ask("  Workspaces Dir", ws_default)
    if ws != defaults.get("WORKSPACES_DIR", "workspaces"):
        out["WORKSPACES_DIR"] = ws
    header_raw = _ask('  Extra HTTP headers for providers (JSON, e.g. {"X-API-Key":"abc"})')
    if header_raw:
        out["PROVIDER_EXTRA_HEADERS"] = header_raw
    _console.print("")


def _section_limits(defaults: dict[str, str], out: dict[str, str], yes: bool) -> None:
    """[limits] Budget + container guardrails."""
    _section(5, "Resource Limits")
    budget_default = defaults.get("DAILY_BUDGET_USD", "10.00")
    mem_default = defaults.get("MAX_CONTAINER_MEMORY_MB", "2048")
    if yes:
        out["DAILY_BUDGET_USD"] = budget_default
        out["MAX_CONTAINER_MEMORY_MB"] = mem_default
        return
    budget = _ask("  Daily Budget USD", budget_default)
    if budget != defaults.get("DAILY_BUDGET_USD", "10.00"):
        out["DAILY_BUDGET_USD"] = budget
    mem = _ask("  Max Container Memory MB", mem_default)
    if mem != defaults.get("MAX_CONTAINER_MEMORY_MB", "2048"):
        out["MAX_CONTAINER_MEMORY_MB"] = mem
    _console.print("")


_SECTION_RUNNERS = {
    "env": _section_env,
    "auth": _section_auth,
    "telegram": _section_telegram,
    "storage": _section_storage,
    "limits": _section_limits,
}


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------


def gather(
    yes: bool = False,
    section: str | None = None,
    skip_test: bool = False,
    profile: str | None = None,
) -> dict[str, str] | None:
    """Walk through the wizard; return env dict or None if user aborts."""
    _banner()
    defaults = _defaults()
    prof_path = profile_path(profile)
    remembered = load_profile(prof_path)
    # .env.local values always win over older remembered answers.
    if remembered:
        defaults = {**remembered, **defaults}
    out: dict[str, str] = {}

    only = section
    if only is not None and only not in SECTIONS:
        _console.print(f"[red]Unknown section '{only}'. Choose from: {', '.join(SECTIONS)}[/red]")
        return None

    def run(name: str) -> bool:
        return only is None or only == name

    if run("env"):
        _section_env(defaults, out, yes)

    # Offer remembered answers once (interactive full runs only).
    if run("providers") and remembered and not yes and only is None:
        prev = remembered.get("DEFAULT_PROVIDER", "echo")
        _console.print(
            f"[dim]Profile {prof_path} remembers default provider [bold]{prev}[/bold].[/dim]"
        )
        if _confirm("  Reuse previous answers (Enter keeps, 'n' to re-ask)?", default=True):
            defaults = {**remembered, **defaults}

    if run("providers"):
        _section_providers(defaults, out, yes, skip_test, remembered)
    if run("auth"):
        _section_auth(defaults, out, yes)
    if run("telegram"):
        _section_telegram(defaults, out, yes)
    if run("storage"):
        _section_storage(defaults, out, yes)
    if run("limits"):
        _section_limits(defaults, out, yes)

    # Never persist the helper key itself (setup_command pops it for the profile).
    _show_summary({k: v for k, v in out.items() if k != "PROVIDERS_PICKED"})
    if yes:
        return out
    if _confirm("  Write to .env.local?", default=True):
        # Remember picks for next time (caller saves the profile on success).
        return out
    return None


def _show_summary(values: dict[str, str]) -> None:
    """Render what will be written, masking secrets (compact 2-up rows)."""
    if not values:
        return
    items = sorted(values.items())
    table = Table(
        box=box.SIMPLE,
        title="[bold]Config → .env.local[/bold]",
        title_justify="left",
        pad_edge=False,
        show_header=False,
    )
    table.add_column("Key", style="bold", no_wrap=True)
    table.add_column("Value")
    for key, value in items:
        display = f"{value[:10]}…" if is_secret_key(key) else value
        table.add_row(key, display)
    _console.print(table)


def _defaults() -> dict[str, str]:
    """Best-effort read existing .env for defaults so re-runs preserve values."""
    out: dict[str, str] = {}
    for path in (pathlib.Path(".env"), pathlib.Path(ENV_LOCAL)):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def write_env(values: dict[str, str]) -> pathlib.Path:
    """Append/merge settings into .env.local. Returns the file path."""
    path = pathlib.Path(ENV_LOCAL)
    existing: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            existing[key.strip()] = value.strip()
    existing.update(values)

    def _quote(value: str) -> str:
        if (
            value.startswith(("#", " "))
            or value.endswith(" ")
            or value.startswith("{")
            or value.endswith("}")
        ):
            return f'"{value}"'
        return value

    lines = [f"{k}={_quote(v)}" for k, v in sorted(existing.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _next_steps(path: pathlib.Path, prof_path: pathlib.Path) -> None:
    """Compact launch + live-reconfiguration guidance."""
    _console.print(f"\n[green]✓[/green] {path} written · profile [dim]{prof_path}[/dim]")
    _console.print()
    use_web = _confirm("Would you like to use the Web dashboard?", default=True)
    if use_web:
        _console.print(
            Panel(
                "[bold]make start[/bold]  launch API + worker + dashboard\n"
                "[bold]agentctl web[/bold]  start the Web dashboard directly\n"
                "\n"
                "[dim]Once running, open:[/dim] [bold]http://localhost:3000[/bold]",
                title="[bold]Web Dashboard[/bold]",
                box=box.ROUNDED,
                border_style="blue",
                padding=(0, 1),
            )
        )
    else:
        _console.print(
            Panel(
                "[bold]make start[/bold]  launch API + worker + dashboard\n"
                "[bold]agentctl chat[/bold]  REPL — reconfigure any time, no restart:\n"
                "  [dim]/model set openrouter meta-llama/…[/dim]  switch model\n"
                "  [dim]/settings set KEY VALUE[/dim]            any config key\n"
                "  [dim]/tools · /skills · /memory · /schedule[/dim]",
                title="[bold]Next[/bold]",
                box=box.ROUNDED,
                border_style="green",
                padding=(0, 1),
            )
        )


def setup_command(
    yes: bool = False,
    section: str | None = None,
    skip_test: bool = False,
    profile: str | None = None,
) -> None:
    """Entrypoint bound to the `setup` Typer command."""
    if section is not None and section not in SECTIONS:
        _console.print(
            f"[red]Unknown section '{section}'. Choose from: {', '.join(SECTIONS)}[/red]"
        )
        raise typer.Exit(code=2)
    values = gather(yes=yes, section=section, skip_test=skip_test, profile=profile)
    if values is None:
        _console.print("[yellow]setup cancelled — nothing written.[/yellow]")
        raise typer.Exit(code=0)
    # Remember non-secret answers (incl. provider picks) for the next run.
    picked = values.pop("PROVIDERS_PICKED", "")
    path = write_env(values)
    prof_path = profile_path(profile)
    remember = dict(values)
    if picked:
        remember["PROVIDERS_PICKED"] = picked
        if "DEFAULT_PROVIDER" not in remember:
            remember["DEFAULT_PROVIDER"] = "echo"
    save_profile(prof_path, remember)
    _next_steps(path, prof_path)


__all__: list[str] = [
    "gather",
    "write_env",
    "setup_command",
    "profile_path",
    "load_profile",
    "save_profile",
    "_masked",
    "_ask",
    "_confirm",
    "_defaults",
    "ENV_LOCAL",
    "SECTIONS",
]
