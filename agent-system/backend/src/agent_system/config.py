"""Application configuration (pydantic-settings).

All resource limits from v3.1 §30 are configurable here and via environment
variables. Secret values are never logged.
"""

from __future__ import annotations

import json as _json
import os as _os
import warnings as _warnings
from functools import lru_cache as _lru_cache
from pathlib import Path

from pydantic import model_validator as _model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Sentinel dev secret — must never be used in production.
DEFAULT_SECRET = "dev-only-secret-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Core
    agent_env: str = "dev"
    api_port: int = 8000
    # Heroku assigns a random $PORT per dyno. `port` binds to it when the
    # platform sets PORT (pydantic-settings maps the PORT env var here);
    # otherwise `api_port` applies (local dev). Use `effective_port`.
    port: int | None = None
    api_session_secret: str = "dev-only-secret-change-me"
    agent_bootstrap_secret: str = "dev-only-secret-change-me"
    redis_url: str = "redis://localhost:6379/0"
    # CORS origins (comma-separated). Settings-driven; defaults to local UI.
    api_cors_origins: str = "http://localhost:3000"

    # Storage
    database_url: str = "sqlite:///data/agent_system.db"
    vault_path: Path = Path.home() / "Downloads" / "Claude memory"
    workspaces_dir: Path = Path("workspaces")
    templates_dir: Path = Path("templates")
    recordings_dir: Path = Path("recordings")
    outputs_dir: Path = Path("outputs")
    # Agent skills (SKILL.md folders; user/agent-added skills live here too).
    skills_dir: Path = Path("skills")
    # Agent soul (identity file injected into every model call).
    # Empty = auto-discover ./SOUL.md, then parent-dir SOUL.md.
    soul_path: str = ""

    # Agent tools (ReAct loop + shell policy). Shell runs sandboxed unless
    # tools_shell_mode=local (explicit host opt-in) or off. Execute tools
    # demand a live approval when tools_require_approval is set.
    tools_shell_mode: str = "sandbox"
    tools_require_approval: bool = True
    tools_max_iters: int = 8
    tools_fs_roots: str = ""
    # Cloud (Heroku): dynos have no Docker daemon, so the sandbox backend is
    # replaced by an in-process subprocess jail (services/sandbox.py).
    # HEROKU_JAIL=true routes shell + execute-risk plugins through the jail
    # instead of Docker. HEROKU_SHELL_ALLOWLIST optionally restricts shell
    # to comma-separated binary prefixes (empty = any binary, approval-gated).
    heroku_jail: bool = False
    heroku_shell_allowlist: str = ""
    # Cloud inline execution (no Redis/RQ worker): Telegram goals are driven
    # in-process via services/cloud.drive_session in a background thread.
    # Local dev keeps this off (make start runs the real RQ worker).
    task_runner_recovery_enabled: bool = True
    cloud_inline_run: bool = False
    # Cloud vault: persist memory notes to the database (memory_notes table)
    # instead of the Obsidian file vault (ephemeral on dynos). Local dev
    # keeps the file vault.
    cloud_vault_db: bool = False
    # Pluggable tools (folder drop, same discovery pattern as SkillManager).
    # Relative paths resolve against the backend working directory.
    tools_plugin_dir: str = "tools_plugins"

    # Memory (Obsidian vault). Outcomes auto-persist; recall_top_k notes
    # are injected into LLM task prompts. Embedding backend: "hash" (zero
    # extra dependencies, offline default) or "local" (sentence-transformers
    # all-MiniLM-L6-v2, opt-in via `pip install agent-system[memory]`).
    memory_auto_remember: bool = True
    memory_recall_top_k: int = 3
    memory_embedding_provider: str = "hash"
    # ReAct context budget (token estimates are chars/4 heuristic, see
    # services/agent_loop.py:estimate_tokens). When accumulated tool-result
    # content exceeds threshold_pct of max_context_tokens, the loop compacts
    # the oldest results instead of blowing the model context window.
    max_context_tokens: int = 100_000
    context_compaction_threshold_pct: float = 75.0

    # Provider circuit breaker: after N consecutive model.failed events for
    # a provider, fail fast (no network) for a cooldown, then half-open probe.
    circuit_breaker_threshold: int = 5
    circuit_breaker_cooldown_seconds: int = 60

    # OpenConnector (oomol-lab/open-connector) — self-hosted connector gateway
    # for 1,000+ SaaS providers via HTTP Actions and MCP. Unset base URL = the
    # openconnector_* tools stay hidden. Tokens optional for local runtimes.
    openconnector_base_url: str = ""
    openconnector_api_key: str | None = None  # back-compat alias for runtime token
    openconnector_runtime_token: str | None = None
    openconnector_admin_token: str | None = None
    openconnector_alias: str = ""  # named connection (x-oo-connector-alias)
    openconnector_user_id: str = "bob-local"  # deprecated (unused by the API)

    # MCP servers (Model Context Protocol, stdio) as JSON:
    #   MCP_SERVERS='[{"name":"fs","command":"npx",
    #     "args":["-y","@modelcontextprotocol/server-filesystem","/tmp"]}]'
    mcp_servers: str = "[]"

    # Agent-to-agent handoff (outbound delegation to an external agent over
    # HTTP with a signed task payload + callback). Off by default; every
    # delegation needs a fresh per-target approval (scope a2a:delegate:* —
    # the bare a2a:delegate scope is default-deny, never ALLOW_ALWAYS).
    a2a_enabled: bool = False

    # Scheduler (cron/interval jobs fire goal sessions; APScheduler).
    scheduler_enabled: bool = True
    scheduler_timezone: str = "UTC"
    # Backups: SQLite online copy + vault/recordings tar snapshot, daily at
    # 03:00 local by default; keep the newest N runs.
    backups_dir: Path = Path("backups")
    backup_retention_count: int = 7
    backup_schedule_cron: str = "0 3 * * *"

    # Telegram gateway (optional)
    telegram_bot_token: str | None = None
    telegram_allowed_chat_ids: str = ""
    telegram_webhook_secret: str | None = None

    # LLM providers (optional; system boots fine without any).
    # OpenAI-style compatibility; per-provider keys + configurable base URLs.
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    groq_api_key: str | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    ollama_base_url: str = "http://localhost:11434/v1"
    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_site_url: str = "https://localhost"
    openrouter_app_name: str = "Bob Agent"
    together_api_key: str | None = None
    together_base_url: str = "https://api.together.xyz/v1"
    mistral_api_key: str | None = None
    mistral_base_url: str = "https://api.mistral.ai/v1"
    gemini_api_key: str | None = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    huggingface_api_key: str | None = None
    huggingface_base_url: str = "https://router.huggingface.co/hf-inference/v1"
    freellmapi_api_key: str | None = None
    freellmapi_base_url: str = "http://localhost:3001/v1"
    tokenrouter_api_key: str | None = None
    tokenrouter_base_url: str = "https://api.tokenrouter.io/v1"

    # Default model routing (used by the ModelRouter when no task rule set).
    default_provider: str = "echo"
    default_model: str | None = None
    # Extra headers sent with every provider call, as JSON:
    #   PROVIDER_EXTRA_HEADERS='{"X-API-Key":"...","User-Agent":"bob-agent/0.1"}'
    provider_extra_headers: str = "{}"

    # Telemetry (optional): unset => fully disabled, zero overhead and zero
    # new required deps. Set => OTLP export (needs agent-system[telemetry]).
    otel_exporter_otlp_endpoint: str = ""

    # Cost
    daily_budget_usd: float = 10.0

    # Resource limits (v3.1 §30)
    max_concurrent_agents: int = 8
    max_concurrent_tasks: int = 16
    max_workspace_size_mb: int = 512
    max_file_size_mb: int = 10
    max_output_size_mb: int = 50
    max_log_size_mb: int = 100
    max_browser_sessions: int = 3
    max_container_cpu: float = 2.0
    max_container_memory_mb: int = 2048
    max_execution_time_seconds: int = 1800
    max_task_tokens: int = 200_000
    max_task_cost_usd: float = 2.0
    max_retries: int = 3

    @property
    def effective_port(self) -> int:
        """Port the server must bind/liveness-probe ($PORT wins on Heroku)."""
        return int(self.port or self.api_port)

    @property
    def cors_origins_list(self) -> list[str]:
        """Parsed CORS origins from the comma-separated setting."""
        return [o.strip() for o in self.api_cors_origins.split(",") if o.strip()]

    @_model_validator(mode="after")
    def _guard_default_secrets(self) -> Settings:
        using_default = any(
            secret.strip() in {"", DEFAULT_SECRET, "change-me-to-a-long-random-string"}
            for secret in (self.api_session_secret, self.agent_bootstrap_secret)
        )
        if using_default:
            _warnings.warn(
                "Using default dev-only secrets; set API_SESSION_SECRET and "
                "AGENT_BOOTSTRAP_SECRET for any shared deployment.",
                RuntimeWarning,
                stacklevel=2,
            )
        env = (self.agent_env or "").lower()
        raw_env = (_os.environ.get("AGENT_ENV", "") or "").lower()
        if (env == "production" or raw_env == "production") and using_default:
            raise RuntimeError(
                "Refusing to start with default dev secrets in production: "
                "set API_SESSION_SECRET and AGENT_BOOTSTRAP_SECRET."
            )
        return self

    @property
    def allowed_chat_ids(self) -> set[int]:
        """Parsed set of allowed Telegram chat ids (empty => nothing allowed)."""
        raw = self.telegram_allowed_chat_ids
        ids: set[int] = set()
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit():
                ids.add(int(part))
        return ids

    @property
    def extra_headers(self) -> dict[str, str]:
        """Parsed JSON of extra headers applied to every provider call."""
        try:
            raw = _json.loads(self.provider_extra_headers)
        except _json.JSONDecodeError:
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items()}

    def provider_api_key(self, provider: str) -> str | None:
        """Return the configured API key for a provider name, if any."""
        return {
            "anthropic": self.anthropic_api_key,
            "openai": self.openai_api_key,
            "groq": self.groq_api_key,
            "openrouter": self.openrouter_api_key,
            "together": self.together_api_key,
            "mistral": self.mistral_api_key,
            "gemini": self.gemini_api_key,
            "deepseek": self.deepseek_api_key,
            "huggingface": self.huggingface_api_key,
            "freellmapi": self.freellmapi_api_key,
            "tokenrouter": self.tokenrouter_api_key,
        }.get(provider)


@_lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    """Clear the cached Settings (for tests that mutate env vars)."""
    get_settings.cache_clear()
