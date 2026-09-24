"""Pure-cloud settings store (no CLI dependency).

The API layer uses this module — never ``agent_system.cli.*`` — so the cloud
runtime boots and serves settings without any CLI package. The CLI remains an
optional admin wrapper that re-exports these helpers.
"""

from __future__ import annotations

import json as _json
import pathlib
from dataclasses import dataclass
from typing import Any

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
    "bob_master_encryption_key": "auth",
    "telegram_bot_token": "telegram",
    "telegram_allowed_chat_ids": "telegram",
    "telegram_allowed_user_ids": "telegram",
    "telegram_webhook_secret": "telegram",
    "telegram_webhook_url": "telegram",
    "heroku_app_name": "telegram",
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
    "heroku_jail": "tools",
    "heroku_shell_allowlist": "tools",
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
    "api_session_secret": "Signs API tokens (minted via POST /api/v1/auth/token).",
    "agent_bootstrap_secret": "Bootstrap secret accepted by POST /api/v1/auth/token.",
    "bob_master_encryption_key": "Dedicated vault KEK (separate from API secret).",
    "telegram_bot_token": "Telegram bot token from @BotFather.",
    "telegram_allowed_user_ids": "Allowlisted Telegram user ids (telegram identity mode).",
    "telegram_webhook_secret": "Secret Telegram sends as X-Telegram-Bot-Api-Secret-Token.",
    "default_provider": "Default model provider.",
}


class SettingsError(ValueError):
    """Raised for unknown keys or invalid values."""


def is_secret_key(env: str) -> bool:
    name = env.strip().upper()
    return name.endswith(("_KEY", "_SECRET", "_TOKEN")) or name in (
        "PROVIDER_EXTRA_HEADERS",
        "MCP_SERVERS",
    )


@dataclass(frozen=True)
class SettingSpec:
    field: str
    env: str
    group: str
    secret: bool
    type_name: str
    help: str


def _base_type(annotation: Any) -> Any:
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
    return _read_env_file(pathlib.Path(".env")), _read_env_file(pathlib.Path(".env.local"))


def effective() -> Any:
    from agent_system.config import Settings

    return Settings()


def mask(value: str) -> str:
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
    if spec.env == "DEFAULT_PROVIDER":
        from agent_system.services.providers import PROVIDERS

        if text not in (*PROVIDERS, "echo"):
            raise SettingsError(f"unknown provider '{text}'. Valid: echo, {', '.join(PROVIDERS)}")
    if spec.env.endswith("_BASE_URL") and text and not text.startswith(("http://", "https://")):
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


def write_env_file(values: dict[str, str], path: pathlib.Path | None = None) -> pathlib.Path:
    target = path or pathlib.Path(".env.local")
    existing: dict[str, str] = {}
    if target.exists():
        for line in target.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            existing[key.strip()] = value.strip()
    existing.update(values)

    def _quote(value: str) -> str:
        starts = {"#", " ", "{"}
        if value.startswith(tuple(starts)) or value.endswith((" ", "}")):
            return f'"{value}"'
        return value

    lines = [f"{k}={_quote(v)}" for k, v in sorted(existing.items())]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def set_setting(key: str, raw_value: str) -> dict[str, Any]:
    from agent_system.config import Settings

    spec = _find_spec(key)
    value = parse_value(spec, raw_value)
    try:
        Settings(**{spec.field: value})
    except Exception as exc:
        raise SettingsError(f"{spec.env} rejected: {exc}") from exc
    write_env_file({spec.env: str(value)})
    return get_setting(spec.env)


def unset_setting(key: str) -> dict[str, str]:
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


def cloud_doctor(settings: Any = None) -> dict[str, Any]:
    """Cloud equivalent of `agentctl doctor` — env-based, no CLI, no secrets."""
    from agent_system.config import get_settings as _get

    s = settings or _get()
    from agent_system.services.providers import configured_providers

    providers = [p["key"] for p in configured_providers(s) if p["configured"]]
    webhook_url = ""
    try:
        webhook_url = s.effective_telegram_webhook_url
    except Exception:
        webhook_url = ""
    allowlist = [
        p for p in str(getattr(s, "telegram_allowed_user_ids", "") or "").split(",") if p.strip()
    ]
    return {
        "agent_env": getattr(s, "agent_env", "?"),
        "identity_mode": getattr(s, "agent_identity_mode", "?"),
        "transport": "webhook" if getattr(s, "telegram_webhook_secret", None) else "polling",
        "telegram_configured": bool(getattr(s, "telegram_bot_token", None)),
        "webhook_url_set": bool(webhook_url),
        "allowlist_count": len(allowlist),
        "allowlist_empty_blocks_all": len(allowlist) == 0,
        "providers_configured": providers,
        "default_provider": getattr(s, "default_provider", "?"),
        "database": (
            "postgres" if str(getattr(s, "database_url", "")).startswith("postgres") else "sqlite"
        ),
        "inline_run": bool(getattr(s, "cloud_inline_run", True)),
        "vault_db": bool(getattr(s, "cloud_vault_db", False)),
        "notes": (
            ["TELEGRAM_ALLOWED_USER_IDS empty → nobody can provision (telegram mode fail-closed)"]
            if len(allowlist) == 0 and str(getattr(s, "agent_identity_mode", "")) == "telegram"
            else []
        ),
    }
