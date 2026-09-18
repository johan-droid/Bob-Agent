<div align="center">

# 🤖 Bob Agent

### Local-First Autonomous Multi-Agent AI System

**You state a goal. Bob decomposes it into a task DAG, executes it with a real
tool registry, and records every step on an inspectable event stream — all
running locally on your machine.**

[Getting Started](#-quickstart) ·
[Features](#-features) ·
[CLI Reference](#-the-cli-agentctl) ·
[Configuration](#-configuration) ·
[API](#-api) ·
[Architecture](#-architecture) ·
[Development](#-development)

</div>

---

## Table of Contents

- [What is Bob?](#what-is-bob)
- [Key Features](#-features)
- [Quickstart](#-quickstart)
- [The CLI (`agentctl`)](#-the-cli-agentctl)
- [Chat REPL](#-chat-repl--the-full-command-system)
- [Configuration](#-configuration)
- [Providers, Skills, Soul & Tools](#-providers-skills-soul--tools)
- [API & Dashboard](#%EF%B8%8F-api--dashboard)
- [Architecture](#-architecture)
- [Tech Stack](#-tech-stack)
- [Project Layout](#-project-layout)
- [Development](#-development)
- [Security](#-security)
- [Contributing](#-contributing)
- [License](#-license)

---

## What is Bob?

Bob is a **local-first autonomous agent system**. Unlike a chat bot that merely
talks, Bob does work: you state a goal, the supervisor decomposes it into an
explicit task DAG, a **ReAct (Reason+Act) LLM agent** executes each task with a
real tool registry (shell, files, web, memory, OpenConnector SaaS actions, MCP
servers), and every step lands on an inspectable event stream.

You can talk to Bob from three surfaces — a terminal REPL, a Next.js dashboard,
or Telegram — and all three speak the same `/api/v1` contracts.

Bob boots **with zero API keys configured** (deterministic `echo` mode) and
works with **12 LLM providers**. It is honest: if no real model runs, results
are labeled as such — no fake "LLM did it" claims.

---

## ✨ Features

| Capability | Description |
|---|---|
| 🧠 **Real LLM task execution** | Goals run through a ReAct loop: the model reasons in text and calls typed tools via fenced ` ```tool:name ` blocks — provider-agnostic, no native function calling required |
| 🌐 **12 LLM providers + offline mode** | OpenAI, Anthropic, Groq, Ollama, OpenRouter, Together, Mistral, Gemini, DeepSeek, HuggingFace, FreeLLMAPI, TokenRouter. Boots with zero keys configured |
| 🧰 **Tool registry** | `shell`, `file_read/write/list`, `web_fetch`, `memory_recall/remember`, `tasks_inspect`, `openconnector_execute/list`, `mcp_call`, `mcp_list`. Shell runs sandboxed by default; execute tools demand live approval when `TOOLS_REQUIRE_APPROVAL` is set |
| 🔌 **OpenConnector integration** | Self-hosted connector gateway (`oomol-lab/open-connector`) — 1,000+ SaaS providers / 10,000+ actions via its HTTP Runtime API and, automatically, as an implicit MCP-over-HTTP server (`POST /mcp`) |
| 🧩 **MCP client (two transports)** | stdio (`npx`/`uvx`/local binaries) and streamable HTTP (JSON + SSE, `Mcp-Session-Id` replay). Configure via `MCP_SERVERS` JSON. Ships with its own **vault server** (`bob-vault-mcp`) that keeps the Obsidian vault updated and maintains a dedicated Bob Agent record |
| 📦 **Pluggable skills** | `SKILL.md` instruction packs that users *and agents* can create, import, enable, and configure |
| 🎭 **SOUL.md identity** | The agent's character, injected into every model call as the `<identity>` block. Edit one file to change who Bob is |
| 💻 **CLI-first operations** | `agentctl` manages sessions, tasks, approvals, workspaces, events, skills, soul, tools, memory, and *every* setting |
| ⚡ **Live configuration** | Switch providers (`/model set`), read/write the Obsidian vault (`/memory`, `/recall`), inspect MCP/connectors (`/tools`), edit any key (`/settings set`) — no restarts needed |
| 🛡️ **Approvals + permission gate** | Risky actions pause for `/approve`, never slip through silently |
| 📹 **Recordings & replay** | Replayable `jsonl` behavior recordings |
| 💰 **Cost tracking** | Per-call cost records, daily budgets, per-task cost caps |
| 📱 **Telegram gateway** | Polling or webhook mode with allowlisted chat IDs |
| 📁 **Obsidian vault memory** | Durable facts with `[[links]]` in your own vault |
| 🔄 **Crash recovery** | Persistent event bus and task state machine that survives restarts |

---

## 🚀 Quickstart

### One command (new machine)

```bash
python3 bootstrap/bootstrap.py            # interactive
python3 bootstrap/bootstrap.py --yes      # fully automatic
```

The bootstrap script detects your OS, installs Python 3.12+ / uv / git / Redis
(via Docker), installs dependencies, migrates the database, and launches the
setup wizard. From a checkout it works offline; published one-liners
(`curl … | bash`, `irm … | iex`) live in `bootstrap/install.sh` / `install.ps1`.

### From a checkout

```bash
make install   # uv sync + editable install
make migrate   # alembic upgrade head
make setup     # interactive wizard → writes .env.local
make start     # Redis + API :8000 + RQ worker
make chat      # interactive REPL (needs make start running)
```

> **First run?** `make setup` (or `agentctl settings wizard`) generates your
> secrets. **Restart `make start` afterwards** — the server reads config at
> boot. `make chat` then authenticates itself automatically.

### Manual setup (without `make`)

```bash
cd backend
uv sync
uv pip install -e .
uv run alembic upgrade head
uv run uvicorn agent_system.api.main:app --port 8000 &   # API server
uv run python -m agent_system.worker                     # RQ worker
uv run agentctl chat                                     # chat REPL
```

Then open `make chat` and type a goal, or use the one-shot form:
`agentctl chat "Summarize this repo"`.

### Platform prerequisites

| Platform | Requirements |
|---|---|
| **Linux** | Docker group membership: `sudo usermod -aG docker $USER && newgrp docker` |
| **macOS** | Docker Desktop required; Rosetta 2 for Apple Silicon |
| **Windows** | Run in PowerShell as Admin; WSL2 recommended |

---

## 💻 The CLI (`agentctl`)

Global flags: `--json` (machine output) · `--no-color` · `--api-url` ·
`--token` (or `AGENTCTL_TOKEN` env). Auth auto-mints from your local
`.env.local` secrets when no token is given.

| Command | What it does |
|---|---|
| `chat ["goal"]` | REPL when bare (`-i` forces it); one-shot submit with a goal (`--watch` tails progress) |
| `setup [--yes] [--section NAME] [--skip-test] [--profile PATH]` | Quick wizard: env check, providers, auth, Telegram, storage, limits → `.env.local` |
| `settings list/get/set/unset/check/wizard/path` | **Every** key and credential. See [Configuration](#-configuration) |
| `doctor` | Probe OS/Python/uv/git/docker/Redis/ports with fix hints |
| `skills list/show/new/add/enable/disable/config/rm` | Manage skill packs |
| `soul show/path/edit` | View and edit the agent identity |
| `sessions / tasks / approvals / workspace / events` | Operate the running system |
| `status / version / config` | Health, version, effective CLI config |

### Chat REPL — the full command system

Every message becomes a goal session; the REPL tails its event stream and
summarizes task outcomes. Slash commands cover **sessions**, **live
configuration**, and **memory/tools** — nothing requires a restart:

```
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
```

`make setup` ends with a **"Next" panel** that tells you exactly this:
`make start` → `agentctl chat` → `/model set` / `/settings set` / `/tools`.

---

## ⚙️ Configuration

Precedence: **environment > `.env.local` > `.env` > defaults.**

Copy `.env.example` to `.env` for shared defaults; secrets belong in
`.env.local` (gitignored) via `make setup`. Non-secret wizard answers are
remembered in `~/.config/bob-agent/setup.json` (`--profile` /
`BOB_PROFILE` override), so re-runs offer to reuse them.

### Key settings (from `.env.example`)

| Group | Keys |
|---|---|
| **Providers** | `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GROQ_API_KEY`, `OLLAMA_BASE_URL`, `OPENROUTER_API_KEY`, `TOGETHER_API_KEY`, `MISTRAL_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `HUGGINGFACE_API_KEY`, `FREELLMAPI_API_KEY`, `TOKENROUTER_API_KEY` |
| **Routing** | `DEFAULT_PROVIDER` (default `echo` = offline), `DEFAULT_MODEL` |
| **Core** | `AGENT_ENV`, `API_PORT`, `API_SESSION_SECRET`, `AGENT_BOOTSTRAP_SECRET`, `REDIS_URL` |
| **Storage** | `DATABASE_URL`, `VAULT_PATH`, `WORKSPACES_DIR`, `TEMPLATES_DIR`, `RECORDINGS_DIR`, `OUTPUTS_DIR`, `SKILLS_DIR` |
| **Tools** | `TOOLS_SHELL_MODE` (sandbox/local/off), `TOOLS_REQUIRE_APPROVAL`, `TOOLS_MAX_ITERS`, `TOOLS_FS_ROOTS`, `TOOLS_PLUGIN_DIR` |
| **Memory** | `MEMORY_AUTO_REMEMBER`, `MEMORY_RECALL_TOP_K`, `MEMORY_EMBEDDING_PROVIDER` |
| **Budgets** | `MAX_CONTEXT_TOKENS`, `DAILY_BUDGET_USD`, `MAX_TASK_COST_USD`, `MAX_TASK_TOKENS` |
| **Limits** | `MAX_CONCURRENT_AGENTS`, `MAX_CONCURRENT_TASKS`, `MAX_WORKSPACE_SIZE_MB`, `MAX_FILE_SIZE_MB`, `MAX_EXECUTION_TIME_SECONDS`, `MAX_RETRIES` |
| **OpenConnector** | `OPENCONNECTOR_BASE_URL`, `OPENCONNECTOR_RUNTIME_TOKEN`, `OPENCONNECTOR_ADMIN_TOKEN`, `OPENCONNECTOR_ALIAS` |
| **MCP** | `MCP_SERVERS` (JSON) |
| **Telegram** | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_IDS`, `TELEGRAM_WEBHOOK_SECRET` |
| **Other** | `CIRCUIT_BREAKER_THRESHOLD`, `BACKUP_RETENTION_COUNT`, `A2A_ENABLED`, `OTEL_EXPORTER_OTLP_ENDPOINT` |

### Optional extras

```bash
uv pip install "agent-system[memory]"     # local embedding backend (sentence-transformers)
uv pip install "agent-system[telemetry]"  # OpenTelemetry instrumentation
```

📚 Full reference: [docs/CONFIGURATION.md](docs/CONFIGURATION.md) ·
[docs/STACK.md](docs/STACK.md) ·
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) ·
[docs/OPERATIONS.md](docs/OPERATIONS.md).

---

## 🧩 Providers, Skills, Soul & Tools

### Providers

Set any `*_API_KEY` (Ollama is keyless at `http://localhost:11434/v1`).
`DEFAULT_PROVIDER`/`DEFAULT_MODEL` pick the default route; `echo` = offline.
`agentctl chat` → `/providers` shows live state; `POST /api/v1/model-routing/test`
or `/model test` validates a key. Switch defaults live with
`/model set <provider> [model]`.

| Provider | Base URL | Example model |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| Anthropic | `https://api.anthropic.com/v1` | `claude-sonnet-4-5` |
| Groq | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` |
| Ollama (local, no key) | `http://localhost:11434/v1` | `llama3.2` |
| OpenRouter | `https://openrouter.ai/api/v1` | `meta-llama/llama-3.3-70b-instruct:free` |
| Together AI | `https://api.together.xyz/v1` | `meta-llama/Llama-3.3-70B-Instruct-Turbo-Free` |
| Mistral | `https://api.mistral.ai/v1` | `open-mistral-7b` |
| Gemini | `https://generativelanguage.googleapis.com/v1beta` | `gemini-2.0-flash` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| HuggingFace | `https://router.huggingface.co/hf-inference/v1` | `meta-llama/Llama-3.2-3B-Instruct` |
| FreeLLMAPI (self-hosted) | `http://localhost:3001/v1` | `auto` |
| TokenRouter | `https://api.tokenrouter.io/v1` | `auto` |

### ReAct + Tools

`agents/react_agent.py` runs task goals through `services/agent_loop.run_tool_loop`.
Tools are typed (JSON schemas), jailed (file roots), and approval-gated
(execute). Without a real provider configured, the deterministic `echo`
builtin runs instead — honest offline mode.

The model emits fenced blocks; the loop executes them and returns results:

```text
You have these tools…
```tool:file_write
{"path": "notes.md", "content": "hello"}
```

<tool_result name="file_write">
{"ok": true}
</tool_result>
```

Every tool execution emits `tool.called` / `tool.result` events (audit trail in
the DB). Unknown tools, approval blocks, and crashes become error results the
model can react to — never silent.

### MCP (Model Context Protocol)

`services/mcp.py` connects to stdio servers (`npx`/`uvx`/bin) **and**
streamable-HTTP endpoints (SSE + JSON, session-id replay) via `MCP_SERVERS`
JSON. Tools: `mcp_list` (discover) + `mcp_call` (invoke). `/tools` in the chat
lists every configured server live.

Bob also ships one server of its own, `bob-vault-mcp`
(`agent_system/mcp_servers/vault.py`, console script `bob-vault-mcp`): it keeps
the Obsidian vault updated — notes, daily log, recall — and maintains a
dedicated Bob Agent record at `records/bob-agent.md`. Attach it with:

```bash
agentctl settings set MCP_SERVERS \
  '[{"name":"vault","command":"uv","args":["run","bob-vault-mcp"],"cwd":"agent-system/backend"}]'
```

Because it is reached through `mcp_call`, every vault write is approval-gated
(`mcp:vault:<tool>`) and audit-logged like any other capability.

### OpenConnector

Set `OPENCONNECTOR_BASE_URL` and run the gateway (`docker compose up
openconnector`) → Bob gains `openconnector_execute` / `openconnector_list`
plus an implicit `openconnector` MCP server. Credentials stay inside
OpenConnector; only the action + input cross the wire. See
[docs/CONFIGURATION.md](docs/CONFIGURATION.md) §OpenConnector.

### Skills

Folders of `SKILL.md` (YAML frontmatter + Markdown) in `backend/skills/` (or
`SKILLS_DIR`). Toggles live in `skills/.state.json`, per-user values in
`skills/<name>/config.local.yaml` — the skill file itself stays pristine.
Users, CLI, API, and agents all create skills the same way (`skills new`,
`skills add <path|url|git>`, `POST /api/v1/skills`). Ships with
`web-research`, `document-craft`, `qa-assist`.

### SOUL.md

[`SOUL.md`](SOUL.md) is prepended as `<identity>` on every model call (missing
file = no block, never an error). Lookup order: `SOUL_PATH` > `./SOUL.md` >
parent-dir `SOUL.md`. Edit one file to change who Bob is.

---

## 🕸️ API & Dashboard

### REST API (`/api/v1`)

| Endpoint | Description |
|---|---|
| `GET /health`, `GET /ready` | Health & readiness |
| `POST /api/v1/auth/token` | Bootstrap secret → Bearer token |
| **Sessions / Tasks** | Goal lifecycle: create, list, inspect, transitions, retries |
| **Approvals** | Pending approvals, decisions, sweep |
| **Workspaces** | Tree, file read/write, exec, fingerprint |
| **Artifacts / Events** | Generated files & the full audit event stream |
| **Recordings / Replay** | Behavior recordings (`.jsonl`) + replay |
| **Batches / Recipes / Personalities** | Batch goals, reusable recipes, persona management |
| **Insights / Model-calls / QA reports** | Analytics and quality reports |
| **Model-routing** | Provider list, test, switch defaults |
| **Skills** | CRUD + import |
| **Schedule / Autopilot** | Scheduled jobs, autopilot status/kill/reset |
| **Telegram** | Webhook + status |
| **Realtime** | `WS`/`SSE` fanout with resume |
| **Settings** | Live config read/write (`GET /api/v1/settings`, `GET+POST /api/v1/settings/{key}`) |
| **A2A** | Agent-to-agent handoff (`POST /api/v1/a2a/delegate`, `POST /api/v1/a2a/callback`, `GET /api/v1/a2a/delegations`; 503 unless `A2A_ENABLED=true`) |
| **Backups** | `agentctl backup run|list|restore` + nightly `nightly-backup` cron (SQLite only; no REST endpoint) |
| **Tool plugins** | Folder-drop tools in `TOOLS_PLUGIN_DIR` (`agentctl tools list|enable|disable`; surfaced via the ReAct registry, no separate REST endpoint) |

### Web Dashboard

`web/` is a Next.js 16 + React 19 dashboard that proxies to the backend:

```bash
cd web
npm install
make web           # or: npm run dev    → http://localhost:3000
```

### Telegram

Polling (default) or webhook mode; only allowlisted chat IDs get responses
(`TELEGRAM_ALLOWED_CHAT_IDS`). Auth is enforced per-message. See
`.env.example` for webhook setup.

---

## 🏗️ Architecture

```
┌─────────────┐   ┌──────────────┐   ┌──────────────┐
│  CLI REPL   │   │  Web UI      │   │  Telegram    │
│  (agentctl) │   │  (Next.js)   │   │  Gateway     │
└──────┬──────┘   └──────┬───────┘   └──────┬───────┘
       │                 │                  │
       └─────────────────┼──────────────────┘
                         ▼
                ┌─────────────────┐
                │   FastAPI /api/ │
                │     /v1/*       │
                └────┬────────────┘
                     │
              ┌──────▼───────┐
              │   Supervisor │  goal → task DAG
              │ (orchestrator)│
              └──────┬───────┘
                     │
              ┌──────▼───────┐          ┌──────────────┐
              │  ReAct Loop  │─────────►│  12 LLM      │
              │ (agent_loop) │   fenced │  providers   │
              └──────┬───────┘   tools  └──────────────┘
                     │
    ┌────────────────┼────────────────────────┐
    ▼                ▼                        ▼
┌────────┐     ┌──────────┐              ┌────────────┐
│ Tools  │     │ MCP      │              │ OpenConn.  │
│ registry│    │ client   │              │ gateway    │
└────────┘     └──────────┘              └────────────┘
    │
    ├── shell (Docker sandbox) ── file_* ── web_fetch
    ├── memory_recall/remember ── tasks_inspect
    └── workspace exec (sandboxed, resource-capped)
```

Service map (`backend/src/agent_system/`):

```
api/main.py            FastAPI app + lifespan (builds all services into app.state)
api/v1/router.py       health/ready/auth-token · sessions · tasks · approvals ·
                       workspaces · artifacts · events
api/v1/features.py     recordings/replay · batches · recipes · personalities ·
                       insights · model-routing · schedule · autopilot ·
                       model-calls · qa-reports
api/v1/realtime.py     realtime router — WS/SSE fanout + resume
api/v1/telegram.py     bot webhook (header-secret) + status
api/v1/skills.py       skills CRUD + import (emits skill.* events)
cli/                   agentctl: main · chat (REPL w/ slash commands incl.
                       live config) · setup (wizard) · settings · skills · soul ·
                       sysdetect
services/orchestrator.py   Supervisor (goal → task DAG validation) + dispatch
services/agent_loop.py     ReAct loop: Reason+Act over the tool registry
services/tools.py          ToolRegistry: shell · file_* · web_fetch ·
                           memory_* · tasks_inspect · openconnector_* · mcp_*
services/mcp.py            MCP client: stdio (npx/uvx) + streamable HTTP
services/openconnector.py  oomol-lab/open-connector Runtime API
services/memory_hooks.py   remember_outcome/fact + recall_recent (vault)
services/model_router.py   ModelRouter.invoke: soul → skills → adapter
services/providers.py      12 real HTTP adapters + pricing + router builder
services/skills.py         SkillManager: discover/compose/configure/author
services/soul.py           SOUL.md lookup + <identity> block
services/telegram.py       polling/webhook gateway, allowlisted chats
services/permissions.py    PermissionGate (risk/scope decisions)
services/auth.py           token mint/verify (bootstrap secret → Bearer)
services/memory.py         Obsidian vault writer + recall + MemoryStore
agents/react_agent.py      llm_react_handler: goal → ModelRouter + run_tool_loop
agents/registry.py         agent_type → handler (+ register_default swap-in)
agents/browser_research.py · documents.py · qa.py   specialist handlers
infra/                     SQLAlchemy engine/session, event bus, models
domain/                    ULIDs, event envelope
worker.py                  RQ worker → agents.react_agent.install() → run_agent
```

### Key flows

1. **Chat goal → execution → answered** — `POST /api/v1/sessions {goal}` → the
   supervisor decomposes into a task DAG → an RQ worker or in-process
   orchestrator runs the ReAct loop → outcome persisted to the vault → REPL /
   dashboard tails `GET /api/v1/events`.
2. **Honest fallback** — with `DEFAULT_PROVIDER=echo`, unregistered agent types
   keep the deterministic builtin; tasks never pretend an LLM ran.
3. **Model call composition** — `[soul <identity>] + prompt + [skills]` →
   provider adapter → `ModelCall` row + `model.*` + `cost.recorded` events.
4. **Workspace + sandbox** — `/workspaces/{id}/exec` → DockerSandbox (CPU/mem
   caps, network off by default) for untrusted steps.

📚 Deep dive: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) ·
[docs/STACK.md](docs/STACK.md).

---

## 🧰 Tech Stack

| Layer | Choice |
|---|---|
| Language | Python ≥ 3.12 |
| Package manager | `uv` |
| API framework | FastAPI ≥ 0.115 + Uvicorn |
| CLI | Typer + Rich |
| Queue | Redis + RQ ≥ 2.12 |
| Database | SQLite (SQLAlchemy 2 + Alembic), migratable to Postgres |
| Validation | Pydantic v2 + pydantic-settings |
| HTTP client | httpx |
| Frontend | Next.js 16 + React 19 + TypeScript 5.5 |
| Document agents | docxtpl, python-pptx, openpyxl, fpdf2, beautifulsoup4 |
| Sandboxing | Docker SDK |
| Quality | pytest, pytest-asyncio, ruff, mypy --strict |

---

## 📁 Project Layout

```
agent-system/
├── README.md  SOUL.md  Makefile  .env.example  docker-compose.yml
├── bootstrap/          # one-shot installer (bootstrap.py, install.sh/ps1)
├── backend/            # FastAPI + RQ worker + agentctl + skills + tests
│   ├── src/agent_system/
│   │   ├── agents/     # react_agent (ReAct LLM) · registry · browser · documents · qa
│   │   ├── services/   # tools · agent_loop · mcp · openconnector · model_router · …
│   │   └── api/ cli/ infra/ domain/        # FastAPI · agentctl · ORM · pure logic
│   ├── skills/         # bundled skill packs (SKILL.md)
│   ├── tools_plugins/  # folder-drop tool plugins
│   ├── tests/          # unit / contract / integration / security / recovery
│   └── alembic/        # DB migrations
├── web/                # Next.js dashboard (proxies /api/v1)
├── docs/               # STACK / ARCHITECTURE / CONFIGURATION / OPERATIONS
├── data/               # SQLite runtime data (gitignored)
├── workspaces/         # isolated coding workspace roots
├── templates/          # workspace template tarballs
├── recordings/         # behavior recordings (.jsonl, replayable)
├── outputs/            # generated files
└── cli/                # thin agentctl launcher (impl lives in the package)
```

---

## 🔧 Development

```bash
make test       # pytest (427+ tests)
make lint       # ruff check + format check
make typecheck  # mypy --strict
make check      # all three
make up / down  # Redis (+ OpenConnector) via docker compose
```

### Backend

- Python 3.12+, deps managed by `uv` (`backend/pyproject.toml`)
- Layout: `backend/src/agent_system/{api,cli,services,agents,infra,domain}`
- Tests in `backend/tests/{unit,contract,integration}`

### Web dashboard

```bash
cd web
npm install
npm run dev          # dev server on :3000
npm run build        # production build
npm start            # production server
```

### Testing the ReAct loop offline

```bash
cd backend
uv run agentctl chat          # echo mode — deterministic, no keys needed
uv run agentctl model test    # ping the default provider
```

---

## 🔒 Security

- **Secrets never leave the machine** — API keys, tokens, and chat IDs are
  masked in every display, never logged.
- **Permission gate** — every tool call is scored for risk & scope;
  irreversible actions require `/approve`.
- **Sandboxed execution** — shell and untrusted code run in Docker with
  CPU/memory caps and network disabled by default.
- **Jailed file tools** — `TOOLS_FS_ROOTS` restricts the file root the agent
  can touch; workspaces enforce traversal-safe roots.
- **Resource limits** — workspace size, file size, output size, log size,
  concurrency, execution time, and per-task cost caps are all configurable.
- **Auth** — bootstrap-secret-minted Bearer tokens for all authenticated
  routes; Telegram webhook uses a header-secret check.

---

## 🤝 Contributing

1. Fork the repository.
2. Create your feature branch: `git checkout -b feat/my-feature`
3. Make your changes.
4. Ensure quality gates pass: `make check`
5. Commit and push, then open a pull request.

Please keep the code style consistent (ruff, mypy --strict), cover new behavior
with tests, and document any new settings.

---

## 📄 License

This project is released under the **MIT License**. See `LICENSE` for details.

---

<div align="center">
Made with ❤️ for local-first AI.
<sub>Talk to Bob from your terminal, your browser, or your chat app.</sub>
</div>