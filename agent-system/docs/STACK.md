# Tech Stack

What Bob is built from, and why. Versions are lower bounds from
`backend/pyproject.toml` / `web/package.json`.

## Runtime

| Layer | Choice | Why |
|---|---|---|
| Language | Python ≥ 3.12 | Modern typing (`X \| None`), stdlib `tomllib`, perf |
| Package manager | `uv` | Fast, lockfile-free sync; `uv run` = one-liner execution |
| API framework | FastAPI ≥ 0.115 | Versioned routers, Pydantic validation, auto-schema |
| Server | Uvicorn (standard) | Async-ready, reload for dev |
| CLI | Typer + Rich | Same `/api/v1` contracts as the dashboard (§34 rule); stable exit codes; tables/panels |
| Queue | Redis + RQ ≥ 2.12 | Durable background execution of agent tasks |
| DB | SQLite (SQLAlchemy 2 + Alembic) | Zero-ops local default; migratable to Postgres via `DATABASE_URL` |
| Validation/settings | Pydantic v2 + pydantic-settings | Typed `Settings` model — the `settings` CLI catalog is derived from it |
| HTTP client | httpx | Provider adapters + CLI transport |
| YAML | PyYAML | `SKILL.md` frontmatter, local config overrides |
| IDs/events | python-ulid | Sortable IDs |

## Backend dependencies (by role)

- **Orchestration:** `rq`, `redis`, `apscheduler` (scheduled jobs)
- **Agent runtime:** `httpx` (OpenConnector + MCP streamable-HTTP transport),
  stdlib JSON-RPC/SSE parsing (no MCP SDK)
- **Documents agents:** `python-docx`, `python-pptx`, `openpyxl`, `fpdf2`, `beautifulsoup4` (research parsing)
- **Sandboxing:** `docker` SDK (container exec/sandbox)
- **Testing/quality:** `pytest`, `pytest-asyncio`, `coverage`, `ruff`, `mypy --strict`, `types-*`

## Frontend

| Layer | Choice |
|---|---|
| Framework | Next.js 16 + React 19 + TypeScript 5.5 |
| Backend link | Same-origin proxy `web/src/app/api/v1/**` → FastAPI (`AGENT_SYSTEM_API_URL`) |

## Data & directories

| Path | Content |
|---|---|
| `data/agent_system.db` | SQLite: sessions, tasks, events, approvals, personalities, model calls |
| `backend/skills/` (or `SKILLS_DIR`) | Skill packs (`SKILL.md` + `config.local.yaml` + `.state.json`) |
| `SOUL.md` (or `SOUL_PATH`) | Agent identity, injected as `<identity>` on every model call |
| `workspaces/` | Isolated coding workspace roots |
| `templates/` | Workspace template tarballs |
| `recordings/` | Behavior recordings (`.jsonl`, replayable) |
| `outputs/` | Generated files |
| Vault (`VAULT_PATH`) | Obsidian memory notes with `[[links]]` (default `~/Downloads/Claude memory`) |
| `.env` / `.env.local` | Shared defaults / local secrets (precedence: env > `.env.local` > `.env` > defaults) |

## External services (all optional)

Redis (or Docker provides it via `docker-compose.yml`), any of the 14 LLM
providers, Ollama for local inference, Telegram Bot
API for the gateway, **OpenConnector** (`ghcr.io/oomol-lab/open-connector`,
spot on `:3000` — 1,000+ SaaS providers via Actions + MCP), and any MCP
server (stdio or HTTP) declared in `MCP_SERVERS`. With none configured, Bob
boots into offline **echo mode** — the ReAct loop still runs deterministically
(no fake LLM results), fully operable and honestly labeled.
