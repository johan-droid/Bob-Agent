# Bob Agent — Developer Guide (single source of truth)

> **One document. Everything.** This is the consolidated engineering reference for the
> Bob Agent local autonomous multi-agent AI system. It documents the entire tech
> stack, the architecture, the implementation of every component, the full API
> surface, the CLI, the frontend, the operational runbook, and the testing story —
> so a developer can onboard and understand *what ships and how it works* from a
> single file.
>
> **Scope:** this describes the **implemented** system (`agent-system/`, branches
> `master`). The companion spec documents live in `../../documentations/` (v3.0/v3.1
> vision & engineering contract) and in this same `docs/` folder
> (`STACK.md`, `ARCHITECTURE.md`, `CONFIGURATION.md`, `OPERATIONS.md`).
> **Source paths** are cited inline (`backend/src/agent_system/...`) so every claim
> maps to a line of code. Verify against the code.

## Table of contents

1. [At a glance](#1-at-a-glance)
2. [Repository layout](#2-repository-layout)
3. [Technology stack (by layer)](#3-technology-stack-by-layer)
4. [Architecture & key flows](#4-architecture--key-flows)
5. [Configuration model](#5-configuration-model)
6. [Data model & persistence](#6-data-model--persistence)
7. [Domain primitives](#7-domain-primitives)
8. [Core implementations](#8-core-implementations)
   - 8.1 [Supervisor + Orchestrator](#81-supervisor--orchestrator)
   - 8.2 [RQ worker, leases & crash recovery](#82-rq-worker-leases--crash-recovery)
   - 8.3 [ReAct agent loop + react_agent + registry](#83-react-agent-loop--react_agent--registry)
   - 8.4 [Tool registry & safety model](#84-tool-registry--safety-model)
   - 8.5 [Model router + 12 providers + echo](#85-model-router--12-providers--echo)
   - 8.6 [MCP client (two transports)](#86-mcp-client-two-transports)
   - 8.7 [OpenConnector (SaaS gateway)](#87-openconnector-saas-gateway)
   - 8.8 [Permission gate & approvals](#88-permission-gate--approvals)
   - 8.9 [Memory + Obsidian vault](#89-memory--obsidian-vault)
   - 8.10 [Skills](#810-skills)
   - 8.11 [Docker sandbox](#811-docker-sandbox)
   - 8.12 [Workspaces + templates](#812-workspaces--templates)
   - 8.13 [Behavior recording + replay](#813-behavior-recording--replay)
   - 8.14 [Error recovery](#814-error-recovery)
   - 8.15 [Cost accounting + budget monitor](#815-cost-accounting--budget-monitor)
   - 8.16 [Task batching](#816-task-batching)
   - 8.17 [Recipes](#817-recipes)
   - 8.18 [Insights](#818-insights)
   - 8.19 [Personality + feedback loop](#819-personality--feedback-loop)
   - 8.20 [Autopilot (desktop automation)](#820-autopilot-desktop-automation)
   - 8.21 [Telegram gateway](#821-telegram-gateway)
   - 8.22 [Auth](#822-auth)
   - 8.23 [Secret redaction](#823-secret-redaction)
   - 8.24 [Tool-fence injection filter](#824-tool-fence-injection-filter)
   - 8.25 [Memory embeddings (hash default, local opt-in)](#825-memory-embeddings-hash-default-local-opt-in)
   - 8.26 [Context compaction](#826-context-compaction)
   - 8.27 [Provider circuit breaker](#827-provider-circuit-breaker)
   - 8.28 [Backups](#828-backups)
   - 8.29 [Pluggable tools](#829-pluggable-tools)
   - 8.30 [Token-level streaming](#830-token-level-streaming)
   - 8.31 [Metrics & tracing](#831-metrics--tracing)
   - 8.32 [Agent-to-agent handoff](#832-agent-to-agent-handoff)
9. [API reference (REST `/api/v1`)](#9-api-reference-rest-apiv1)
10. [Realtime transport (WS + SSE)](#10-realtime-transport-ws--sse)
11. [CLI — `agentctl`](#11-cli--agentctl)
12. [Frontend — Next.js dashboard](#12-frontend--nextjs-dashboard)
13. [Operational runbook](#13-operational-runbook)
14. [Testing strategy](#14-testing-strategy)
15. [Bootstrapping & first run](#15-bootstrapping--first-run)
Appendix A. [Environment variable catalog](#appendix-a-environment-variable-catalog)
Appendix B. [Canonical event types](#appendix-b-canonical-event-types)
Appendix C. [Glossary](#appendix-c-glossary)

---

## 1. At a glance

Bob is a **Telegram-controlled cloud-based autonomous multi-agent AI system**. You state a goal via Telegram; a
supervisor decomposes it into an explicit task DAG; ReAct LLM agents execute
each task with a real tool registry (shell, files, web, memory, OpenConnector SaaS
actions, MCP servers), and every step lands on an inspectable, replayable event
stream and PostgreSQL outbox for Telegram delivery. Telegram is the primary control
interface while cloud infrastructure performs execution and stores durable state.

**Headline capabilities (implemented):**

- ReAct agent execution via **provider-agnostic fenced ` ```tool:name ` blocks** —
  works on OpenAI, Anthropic, Gemini, Groq, Ollama, etc., and an honest offline
  `echo` mode (no fake LLM results). Source: `services/agent_loop.py`,
  `agents/react_agent.py`.
- **12 LLM providers** with per-task routing + pricing/cost tracking.
- **Tool registry** with a permission gate / approval flow for `execute` tools.
- **OpenConnector** integration (1,000+ SaaS providers) via HTTP Runtime API **and**
  an implicit MCP-over-HTTP server.
- **MCP client** — stdio + streamable HTTP (JSON + SSE, session-id replay).
- **Pluggable skills** (`SKILL.md` instruction packs), **SOUL.md** identity,
  **Obsidian vault** memory.
- **SQLite** authoritative store (WAL, FK on, busy timeout, transactions), Redis
  for queue/coordination only, RQ workers.
- **Approvals, recordings + replay, recipes, batches, cost budgets, insights,
  personalities, autopilot (off by default), Telegram, scheduler.**

## 2. Repository layout

```
agent-system/                       # repo root (working dir)
├── README.md, SOUL.md, Makefile, .env.example, docker-compose.yml
├── bootstrap/                      # one-shot installer: bootstrap.py, install.sh, install.ps1
├── backend/                        # ← the application (Python 3.12 + uv)
│   ├── pyproject.toml              # deps + scripts (agentctl entrypoint) + tool config
│   ├── uv.lock                     # pinned transitive deps
│   ├── alembic/                    # migrations (env.py, script.py.mako, versions/)
│   ├── src/agent_system/
│   │   ├── api/     FastAPI app: main.py(lifespan), deps.py, v1/{router,features,realtime,skills,telegram}.py
│   │   ├── cli/     agentctl: main.py, chat.py(REPL), settings.py, skills.py, soul.py, setup.py, sysdetect.py
│   │   ├── services/    orchestrator, agent_loop, tools, model_router, providers, mcp,
│   │   │                  openconnector, permissions, memory, memory_hooks, skills,
│   │   │                  sandbox, workspaces, recording, recovery, batching,
│   │   │                  recipes, insights, personality, autopilot, telegram,
│   │   │                  auth, secrets, soul
│   │   ├── agents/    react_agent.py(ReAct LLM), registry.py, browser_research.py, documents.py, qa.py
│   │   ├── infra/     db.py, event_bus.py, models.py, repositories/
│   │   ├── domain/    ids.py, events.py, tasks.py, lifecycles.py  (pure logic)
│   │   └── worker.py  `python -m agent_system.worker` (RQ entrypoint)
│   ├── skills/                     # shipped skill packs: web-research, document-craft, qa-assist
│   └── tests/                      # 492 tests: unit/, contract/, integration/, recovery/, security/
├── web/                            # Next.js 16 + React 19 + TypeScript (dashboard)
│   ├── package.json, next.config.mjs, tsconfig.json, smoke-test.mjs
│   └── src/{app/{pages + api/v1/[...path]/route.ts}, components/, lib/api.ts}
├── docs/                           # STACK/ARCHITECTURE/CONFIGURATION/OPERATIONS + this file
├── data/ workspaces/ templates/ recordings/ outputs/   # runtime data dirs
└── cli/agentctl.py                 # thin launcher: `python3 cli/agentctl.py …`
```

Runtime flow (one line): **Goal** (CLI/chat/Telegram) → `POST /sessions` →
Supervisor builds a task DAG in SQLite → tasks QUEUED → RQ **worker** (or in-process
**orchestrator**) pulls them → `llm_react_handler` runs the **ReAct loop**
(`ModelRouter.invoke` + `run_tool_loop`) → tools hit shell/files/web/memory/MCP/
OpenConnector, each gated by the **PermissionGate** → every state change emits a
canonical **Event** persisted by the **EventBus**, fanned out over **WS/SSE** →
dashboard + REPL tail the stream live.

The vision/contract docs (`../../documentations/00_Index.md` → `99_Master_Build_Plan.md`)
describe the v3.0/v3.1 roadmap; the implemented system is `agent-system/`.

## 3. Technology stack (by layer)

Versions are lower bounds from `backend/pyproject.toml` / `web/package.json`
(exact pinned versions live in `uv.lock` / `package-lock.json`).

**Runtime**

| Layer | Choice | Why |
|---|---|---|
| Language | Python ≥ 3.12 | modern typing, stdlib `tomllib` |
| Package manager | `uv` | fast sync + `uv run` one-liner execution |
| API framework | FastAPI ≥ 0.115 | versioned routers, Pydantic validation, auto-schema |
| Server (ASGI) | Uvicorn (standard) | async-ready, `--reload` for dev |
| CLI | Typer + Rich | same `/api/v1` contracts as the dashboard; styled tables/panels |
| Queue / coordination | Redis + RQ ≥ 2.12 | durable background task execution |
| Scheduler | APScheduler | cron/interval/date/webhook jobs |
| DB (authoritative) | SQLite (SQLAlchemy 2 + Alembic) | zero-ops local default; migratable via `DATABASE_URL` |
| Settings / validation | Pydantic v2 + pydantic-settings | typed `Settings` — the `settings` CLI catalog is derived from it |
| IDs / events | python-ulid, structlog | sortable IDs, structured logs |
| HTTP client | httpx | provider adapters + CLI/API transport |
| YAML | PyYAML | `SKILL.md` frontmatter, local config overrides |

**Backend dependencies by role** (`pyproject.toml` `[project].dependencies` + dev group)

- *Orchestration:* `rq`, `redis`, `apscheduler`
- *Agent runtime:* `httpx`, stdlib JSON-RPC/SSE parsing (no MCP SDK)
- *Documents agent:* `python-pptx`, `python-docx` (transitive via `docxtpl`), `openpyxl`, `fpdf2`, `beautifulsoup4`
- *Sandboxing:* `docker` SDK
- *Quality:* `pytest`, `pytest-asyncio`, `coverage`, `ruff`, `mypy --strict`, `types-*`

**Frontend** (`web/package.json`)

| Layer | Choice |
|---|---|
| Framework | Next.js 16 + React 19 + TypeScript 5.5 |
| Backend link | same-origin proxy `web/src/app/api/v1/**` → FastAPI (`AGENT_SYSTEM_API_URL`) |

**Data & directories**

| Path | Content |
|---|---|
| `data/agent_system.db` | SQLite: sessions, tasks, events, approvals, personalities, model calls, … |
| `backend/skills/` (or `SKILLS_DIR`) | skill packs (`SKILL.md` + `config.local.yaml` + `.state.json`) |
| `SOUL.md` (or `SOUL_PATH`) | agent identity, injected as `<identity>` on every model call |
| `workspaces/` | isolated coding workspace roots |
| `templates/` | workspace template tarballs |
| `recordings/` | behavior recordings (`.jsonl`, replayable) |
| `outputs/` | generated files (pptx/pdf/…) |
| Vault (`VAULT_PATH`) | Obsidian memory notes with `[[links]]` |
| `.env` / `.env.local` | shared defaults / local secrets — env > `.env.local` > `.env` > defaults |

## 4. Architecture & key flows

### 4.1 Service topology

The FastAPI app is assembled in one place — `api/main.py` `lifespan()` — which wires
every real service into `app.state`:

```python
app.state.settings          # Settings (pydantic-settings)
app.state.session_factory   # SQLAlchemy session factory (SQLite, WAL)
app.state.event_bus         # EventBus (canonical event pipeline)
app.state.gate              # PermissionGate (in-memory approvals)
app.state.authenticator     # Authenticator (Bearer token mint/verify)
app.state.telegram          # TelegramService (optional; 503 if unconfigured)
app.state.skill_manager     # SkillManager (pluggable skills)
app.state.soul_path/.soul_text   # SOUL.md loader
app.state.model_router      # ModelRouter (12 providers + echo)
app.state.autopilot         # AutopilotService (OFF by default)
```

Routers mount under `/api/v1` and share the `authenticated` dependency
(`api/deps.py:get_authenticator` → `services/auth.py:require_auth`):

- `api/v1/router.py` — health/ready/auth-token · sessions · tasks · approvals ·
  workspaces · artifacts · events
- `api/v1/features.py` — recordings/replay · batches · recipes · personalities ·
  insights · model-routing · schedule · autopilot · model-calls · qa-reports
- `api/v1/realtime.py` — WS `/ws/events` + SSE `/events/stream` (resume-from-seq)
- `api/v1/skills.py` — skills CRUD + import (`/api/v1/skills`)
- `api/v1/telegram.py` — bot webhook + status

### 4.2 Key flows

**Chat goal → execution → answered.** `POST /sessions {goal}` → session `ACTIVE` +
`session.created` → Supervisor decomposes server-side into a task DAG → tasks land
`QUEUED` → either the RQ worker (`python -m agent_system.worker`) or the in-process
orchestrator picks one up → for a goal-bearing task, `llm_react_handler` runs the
**ReAct loop**:

`ModelRouter.invoke` → provider adapter → `model.completed/failed` + `ModelCall` row +
`cost.recorded` → `run_tool_loop` reads the output, executes fenced
` ```tool:name ` calls through the `ToolRegistry`, feeds `<tool_result>` back until the
model answers or the iteration budget (`tools_max_iters`, default 8) is spent.

**Tool safety.** Every tool declares a risk tier (`read`/`write`/`execute`).
`execute` tools (shell) run in the Docker sandbox by default
(`tools_shell_mode=sandbox`); `local` is an explicit opt-in, `off` disables shell.
When `tools_require_approval` is set, execute tools raise `NeedsApprovalError`,
persisting an `approval.requested` (Sensitivity=SENSITIVE) row; the user resolves with
`/approve`, then the task is retried. File tools are jailed to allowed roots and refuse
secret paths. Unknown tools, approval blocks, and crashes become error `tool_result`
the model can react to — never silent.

**Model call composition (the only prompt seam).** `ModelRouter.invoke` builds the
prompt as `[soul <identity>] + prompt + [skills <skills>]` → adapter
(OpenAI-compatible `/chat/completions`, Gemini `:generateContent` w/ `x-goog-api-key`,
Anthropic `/v1/messages` w/ `x-api-key` + `anthropic-version`) → records
`model.completed/failed` (with `skills_used`, `soul_used`) + `cost.recorded`. No
skill manager and no soul file ⇒ the prompt passes through untouched.
`react_agent._build_router` self-heals offline defaults (zero-cost echo pricing +
`EchoProvider`) so `DEFAULT_PROVIDER=echo` is runnable end-to-end.

**Event bus.** `services/orchestrator.py`, `services/agent_loop.py`, `services/tools.py`
and the API all emit canonical `Event`s via `infra/event_bus.py:EventBus.emit()`, which
assigns a monotonic sequence, persists to SQLite (`events` table), redacts secret keys,
and dedupes duplicate `event_id`s. `realtime.py` fans them out over **WS** and **SSE**
with `after_sequence` replay so reconnecting clients lose nothing.

**Workspace + sandbox exec.** `POST /workspaces/{id}/exec` → `DockerSandbox.run()`
(CPU/mem/pids caps, network off by default, 10 MB output cap, time limit) with the
workspace bind-mounted at `/ws`. Runs as the invoking user so files stay host-owned.

## 5. Configuration model

**Precedence:** environment variable > `.env.local` > `.env` > built-in default.
`.env.example` documents every key. Manage it without editing files:

```bash
agentctl settings list [--group core|providers|routing|auth|telegram|storage|tools|memory|integrations|scheduler|cost|limits]
agentctl settings get GROQ_API_KEY
agentctl settings set DAILY_BUDGET_USD 7.5
agentctl settings unset MAX_RETRIES
agentctl settings check                 # validates the effective Settings
agentctl settings wizard [--yes]        # exhaustive first-run pass
make setup                              # quick wizard path
```

`settings set` validates the value through the `Settings` model before writing, so
the catalog can never drift from the code. Secrets are masked unless `--show-secrets`.
The chat REPL mirrors all of this live (no restart): `/settings`, `/model set`,
`/tools`, `/schedule`, `/soul`, `/skills`.

The `Settings` class (`config.py`) is the single source of truth. Field groups:

| Group | Key fields (env var upper-cases the attr) |
|---|---|
| core | `agent_env`, `api_port`, `api_session_secret`, `agent_bootstrap_secret`, `redis_url` |
| storage | `database_url` (default `sqlite:///data/agent_system.db`), `vault_path`, `workspaces_dir`, `templates_dir`, `recordings_dir`, `outputs_dir`, `skills_dir`, `soul_path` |
| providers | keys+base_urls for **12 providers** (see §8.5): `anthropic_api_key`, `openai_api_key`, `groq_api_key`, `groq_base_url`, `ollama_base_url`, `openrouter_api_key`, `openrouter_base_url`, `together_api_key`, `mistral_api_key`, `gemini_api_key`, `deepseek_api_key`, `huggingface_api_key`, `freellmapi_api_key`, `tokenrouter_api_key`, `default_provider=echo`, `default_model` |
| routing | `provider_extra_headers` (JSON blob) |
| tools | `tools_shell_mode` (sandbox\|local\|off), `tools_require_approval` (bool), `tools_max_iters` (8), `tools_fs_roots` |
| memory | `memory_auto_remember` (true), `memory_recall_top_k` (3) |
| integrations | `openconnector_base_url`, `openconnector_runtime_token`, `openconnector_admin_token`, `openconnector_alias`, `mcp_servers` (JSON list) |
| scheduler | `scheduler_enabled` (true), `scheduler_timezone` (UTC) |
| telegram | `telegram_bot_token`, `telegram_allowed_chat_ids` (csv), `telegram_webhook_secret` (empty ⇒ long-polling) |
| cost | `daily_budget_usd` (10.0) |
| limits (v3.1 §30) | `max_concurrent_agents`, `max_concurrent_tasks`, `max_workspace_size_mb`, `max_file_size_mb`, `max_output_size_mb`, `max_log_size_mb`, `max_browser_sessions`, `max_container_cpu`, `max_container_memory_mb`, `max_execution_time_seconds`, `max_task_tokens`, `max_task_cost_usd`, `max_retries` |

`Settings.provider_api_key(name)` maps a provider key → its `*_API_KEY`.

## 6. Data model & persistence

**SQLite is authoritative** (WAL, FK on, busy_timeout=5000, `synchronous=NORMAL`).
Redis is queue/coordination only — never authoritative (`28_Reliability_Operations.md`).
All writes go through `infra/db.py:session_scope()` (transactional: commit/rollback/
close). Identity is **prefixed ULIDs** (`domain/ids.py`: `ses_`, `task_`, `run_`,
`evt_`, `toolcall_`, `modelcall_`, `approval_`, `ws_`, `art_`, `mem_`, `rec_`,
`recipe_`, `batch_`, `qa_`, `insight_`).

Schema is Alembic-managed (`alembic/versions/`): `94be8eadb99f` =
v3.1 canonical schema; `7c1a2d9e4f50` = task `result_json`. Run `make migrate`.

**ORM models** (`infra/models.py`) map 1:1 to tables. Key tables:

| Table | Purpose |
|---|---|
| `sessions` | `id, goal, status, created_at, completed_at` |
| `tasks` | DAG nodes: `id, session_id, task_type, title, input_json, depends_on_json, state, agent_type, batch_id, idempotency_key, attempt, result_json, last_error, created_at, updated_at, started_at, completed_at` (unique on `idempotency_key`) |
| `agent_runs` | `id, task_id, agent_type, state, pid, worker_id, started_at, ended_at, result_json, error_json` |
| `agent_leases` | lease/heartbeat: `agent_run_id, worker_id, state, heartbeat_at, lease_expires_at` |
| `approvals` | permission rows `id, task_id, agent_run_id, requested_action, risk, scope, requester, decision, decided_by, reason, created_at, decided_at, expires_at` |
| `workspaces` | `id, name, container_id, status, size_bytes, template_id, cloned_from_template, created_at, last_modified_at` |
| `artifacts` | `id, task_id, kind (pptx/pdf/docx/xlsx/file), path, size_bytes, created_at` |
| `events` | append-only canonical log `event_id, schema_version, session/task/agent_run ids, sequence, timestamp, type, actor, payload, visibility, sensitivity` (idx: `session_id`, `type`, `session_id+sequence`) |
| `model_calls` | `id, task/agent_run ids, provider, model_id, requested_at, completed_at, status, tokens_*, usage_is_estimated, cost_usd, cost_is_estimated, latency_ms, error_json` |
| `tool_calls` | `tool_call_id, agent_run_id, task_id, tool_name, started/completed_at, status, risk, approval_id, result_json` |
| `scheduled_jobs` | `id, name, kind, schedule_json, payload_json, enabled, created_at` |
| `cost_budget` | `id, period, limit_usd, period_start, alert_threshold_pct, created/updated_at` |
| `task_batches` | `id, batch_type, task_ids_json, member_count, created/completed_at, speedup_factor` |
| `qa_reports` | `id, task_id, code_file, tests_*, duration_ms, coverage_pct, diagnostics_json, created_at` |
| `recipes` | `id, name, description, task_dag_json, parameters_json, tags_json, version, executions_count, created_at` |
| `agent_personalities` / `feedback_log` | tone/verbosity/reasoning_style + ratings |
| `insights` | `id, type, generated_at, content_html, key_findings_json, archived_at` |
| `behavior_recordings` | `id, session/agent_run ids, recording_start/end, action_count, action_log_path` |
| `workspace_templates` | `id, name, source_workspace_id, snapshot_path, git_history_json, tags_json, created_at, used_count` |
| `idempotency_keys` | `key, operation, created_at, result_ref` |

## 7. Domain primitives

Pure-logic modules under `domain/` — no I/O, no framework.

**Identifiers** (`domain/ids.py`): every entity gets a prefixed ULID via `new_id(prefix)`.
`new_session_id→ses_`, `new_task_id→task_`, `new_agent_run_id→run_`, `new_event_id→evt_`,
`new_tool_call_id→toolcall_`, `new_model_call_id→modelcall_`, `new_approval_id→approval_`,
`new_workspace_id→ws_`, `new_artifact_id→art_`, `new_memory_id→mem_`, `new_recipe_id→recipe_`,
`new_batch_id→batch_`, `new_qa_report_id→qa_`, `new_insight_id→insight_`.

**Events** (`domain/events.py`): the canonical envelope. One bus, one envelope:
`Event(event_id, schema_version=1, session_id?, task_id?, agent_run_id?, sequence?`
(assigned by EventBus on persist), `timestamp(utcnow)`, `type`, `actor="system"`,
`payload`, `visibility` (USER|INTERNAL), `sensitivity` (NORMAL|SENSITIVE).
`EventVisibility`/`EventSensitivity` are `StrEnum`s. `utcnow()` = `datetime.now(UTC)`.

**Task state machine** (`domain/tasks.py`): `PENDING → PLANNING → QUEUED → RUNNING →
{BLOCKED_APPROVAL|RECOVERING|REVIEW|SUCCEEDED|FAILED|CANCELLED}`. Transitions are
explicit (`TRANSITIONS` dict) and **rejected**, never coerced —
`validate_transition(current, target)` raises `InvalidTransitionError`. `attempt`
counts *starts* and increments only at the RUNNING transition. Terminal =
SUCCEEDED|FAILED|CANCELLED.

**Agent lifecycle** (`domain/lifecycles.py`): `CREATED → INITIALIZING → READY → RUNNING ↔
WAITING_TOOL | WAITING_APPROVAL | RECOVERING → COMPLETED|FAILED|TERMINATED`. Same
explicit-transition rules. Heartbeat/lease tracking lives in `infra` (`AgentLease`).

## 8. Core implementations

### 8.1 Supervisor + Orchestrator

`services/orchestrator.py`. Two cooperating pieces:

- **`Supervisor`** — goal decomposition into a DAG. `create_session()` makes a session
  + `session.created` event; `add_task()` appends `Task` rows with `depends_on_json`
  (validates each dep exists; rejects > `MAX_TASKS_PER_SESSION=100` tasks); `plan()`
  validates the DAG (cycle detection, unknown deps) and flips ready tasks
  PENDING→QUEUED, emitting `task.queued`.
- **`Orchestrator`** — executes tasks via agent handlers, owns
  `AgentRun` rows + `AgentLease` heartbeats (`LEASE_TTL_SECONDS=30`), cancels tasks
  (cooperative via `_cancel_requested`), and **crashes recoverably**
  (`recover_orphans()` finds leases past expiry → RECOVERING→QUEUED on failure while
  `attempt < 3`, else FAILED → `recovery.started/completed/failed` events). Falls back
  to `react_agent` for unregistered agent types.

Task execution: the worker or in-process orchestrator pulls a `QUEUED` task, transitions
it RUNNING (incrementing `attempt`), runs `agent_loop.run_agent`, and on completion
records `task.completed`/`task.failed` + `AgentRun` end state + deletes the lease.
SQLite is re-derived from in the worker (the queue only ever carries task ids).

### 8.2 RQ worker, leases & crash recovery

`worker.py` — out-of-process entrypoint: `python -m agent_system.worker`.

- Queue name `agent-system`; `enqueue_task()` pushes `execute_task` with
  `job_timeout=max_execution_time_seconds` (default 1800s).
- A background **heartbeat thread** refreshes `AgentLease.lease_expires_at` every 15s
  while the run is alive. DB death ⇒ thread stops ⇒ the lease expires ⇒ the
  orchestrator's lease reaper reclaims it (idempotent, no double-counting of `attempt`).
- `execute_task()`: loads task (skips if already handled), QUEUED→RUNNING, installs
  `react_agent` handlers (`react_agent.install()`), runs via
  `agents.registry.run_agent(agent_type, input, ctx)`, then SUCCEEDED (sets
  `result_json`, emits `task.completed`) or FAILED (`_finish_failed` records
  `last_error`, `error_json`, emits `task.failed`). Engine disposed in `finally`.
- Worker name is `agent-worker-{host}-{uuid}` so a crashed worker's stale
  registration never blocks a fresh one (RQ raises on duplicate names).

### 8.3 ReAct agent loop + react_agent + registry

The agent runtime is split cleanly:

- **`agents/react_agent.py`** — the `llm` agent handler. `_build_router()` assembles a
  `ModelRouter` (registers adapters for configured providers + the `EchoProvider`),
  defaulting to `DEFAULT_PROVIDER=echo` so the loop is runnable offline.
  `llm_react_handler(task_input, context)` resolves the goal (from `goal`/`prompt`/
  `instruction`/`task`/`title`), builds a `ToolContext`, builds the tool registry, and
  calls `run_tool_loop(invoke=..., system=_SYSTEM_PROMPT, task=<goal+merged recall>,
  registry, ctx, emit, max_iters)`. After success it calls
  `memory_hooks.remember_outcome()` to auto-write a scrubbed vault note. It is the
  **goal-aware fallback** for unregistered agent types when a real provider is set
  (`react_agent.install()` registers it + `dispatch_default`).
- **`services/agent_loop.py`** — the ReAct loop itself (`run_tool_loop`). It is
  provider-agnostic: the model reasons in text and emits fenced
  ` ```tool:name {json} ` blocks; the loop parses them (`TOOL_FENCE_RE`), executes each
  via the registry, and feeds `<tool_result>` JSON back, looping until the model answers
  or `max_iters` is reached. `parse_tool_calls()` tolerates malformed JSON (→ `{"_raw":…}`),
  `strip_tool_calls()` hides the protocol from the final answer. Every call emits
  `tool.called`/`tool.result` via `emit`. Returns `LoopResult(output, tool_calls,
  iterations, usage, stopped=done|max_iters|error)`. Because the protocol lives in
  prompt text, it works identically on every provider including echo.
- **`agents/registry.py`** — `agent_type → handler` map with a swappable default
  (`register_default`). `_builtin` is the honest deterministic handler (sleeps per
  `simulate_seconds`, echoes input keys) used when no richer handler matches and no real
  provider is configured — **never fabricates work**. `run_agent()` resolves + invokes.

### 8.4 Tool registry & safety model

`services/tools.py` — OpenClaw-style "Hands". A `Tool(name, description, parameters,
risk, handler)` where `risk ∈ {read, write, execute}`. `ToolRegistry` holds
name→Tool, renders a compact `prompt_block()` into the ReAct system prompt, and
`build_registry(settings)` conditionally registers tools.

**Registered tools** (`shell`, `file_read`, `file_write`, `file_list`, `web_fetch`,
`memory_recall`, `memory_remember`, `tasks_inspect`, `openconnector_execute`,
`openconnector_list`, `mcp_call`, `mcp_list`).

**Safety model** (mirrors OpenClaw policy):
- `execute` tools = `shell`. `tools_shell_mode=sandbox` ⇒ run in `DockerSandbox`
  (`services/sandbox.py`); `local` is an explicit host opt-in; `off` disables it.
- When `tools_require_approval`, execute tools persist an `approval.requested` row and
  raise `NeedsApprovalError` (carries the `approval_id`) → task fails honestly, user
  `/approve`s, task is retried.
- `file_*` tools are jailed to `tools_fs_roots` / workspace root and **refuse secret
  paths** (`services/secrets.py:is_secret_path`).
- `openconnector_*` only appear when `OPENCONNECTOR_BASE_URL` is set; `mcp_*` only when
  `MCP_SERVERS` is configured (or OpenConnector implicit server).
- Every call records `tool.called`/`tool.result`; errors surface as `tool_result` the
  model can react to — never silent.

### 8.5 Model router + 12 providers + echo

`services/model_router.py` + `services/providers.py`. The router is the **only** prompt
seam: `ModelRouter.invoke(factory, model_id, prompt, …, skills=, agent_type=)`
composes `[<identity> soul] + prompt + [skills]`, calls the adapter, and atomically
records a `ModelCall` row + `model.requested`/`model.completed|failed` events +
`cost.recorded`. Unknown/estimated usage is flagged, never fabricated; provider errors
are wrapped as `model.failed`, never crash execution.

**Provider catalog** (`providers.py:PROVIDERS`, a frozen `ProviderSpec` registry) —
transport per provider, 12 real options:

| Key | Transport | Auth | Default model |
|---|---|---|---|
| `openai` | OpenAI-compatible | bearer | `gpt-4o-mini` |
| `anthropic` | **native** `/v1/messages` (x-api-key + anthropic-version) | custom | `claude-sonnet-4-5` |
| `groq` | OpenAI-compatible | bearer | `llama-3.3-70b-versatile` (free tier) |
| `ollama` | OpenAI-compatible | none (local) | `llama3.2` (free tier) |
| `openrouter` | OpenAI-compatible | bearer | `meta-llama/llama-3.3-70b-instruct:free` (free tier) |
| `together` | OpenAI-compatible | bearer | `meta-llama/Llama-3.3-70B-Instruct-Turbo-Free` (free tier) |
| `mistral` | OpenAI-compatible | bearer | `open-mistral-7b` (free tier) |
| `gemini` | **native** `:generateContent` (x-goog-api-key) | header | `gemini-2.0-flash` (free tier) |
| `deepseek` | OpenAI-compatible | bearer | `deepseek-chat` |
| `huggingface` | OpenAI-compatible | bearer | `meta-llama/Llama-3.2-3B-Instruct` (free tier) |
| `freellmapi` | OpenAI-compatible | bearer (self-hosted) | `auto` (free tier) |
| `tokenrouter` | OpenAI-compatible | bearer | `auto` |

Adapters are pure-HTTP `httpx` clients (no heavy SDKs). `build_model_router()`
registers adapters only for **configured** providers (keyless ones like `ollama`/`
freellmapi` register without a key). `build_pricing()` registers **free-tier** models
at real `$0.00` cost; billable models are left unregistered so the router flags their
cost as estimated. Defaults: `default_provider=echo`, which self-heals to
`EchoProvider` (deterministic, `$0`, no network) + `UnavailableProvider` (for recovery
tests). `POST /api/v1/model-routing/providers` exposes the catalog; `POST
/api/v1/model-routing/test` pings a provider.

### 8.6 MCP client (two transports, one interface)

`services/mcp.py` — stdlib + httpx, **no MCP SDK dependency**. `McpServerConfig`
dispatches on transport: `command`+`args` ⇒ stdio (`McpStdioClient`), `url` ⇒ streamable
HTTP (`McpHttpClient`). The HTTP client speaks JSON-RPC over `POST`, parses **both**
`application/json` and `text/event-stream` SSE `event:/data:` frames, replays
`Mcp-Session-Id`, runs the `initialize` handshake (protocol `2025-03-26`), and treats
`202` notifications tolerantly. Configure via `MCP_SERVERS` JSON (env or
`/settings set`). **Fresh session per call; failures recorded, never fatal.**
`all_servers()` = configured `MCP_SERVERS` + an **implicit `openconnector` HTTP MCP
server** (see §8.7); an explicit entry named `openconnector` wins. Tools exposed to the
ReAct loop: `mcp_list` (discover) + `mcp_call` (invoke).

Bob also ships one MCP *server*: `agent_system/mcp_servers/vault.py` (console
script `bob-vault-mcp`, `uv run bob-vault-mcp --vault <path>`). It speaks
newline-delimited JSON-RPC 2.0 on stdin/stdout and exposes
`vault_write_note`, `vault_append_daily`, `vault_record`, `vault_read_record`,
`vault_recall`, `vault_status`. It exists so the vault keeps updating itself
through the *same* seam as any external capability: Bob attaches it with a
`MCP_SERVERS` entry and reaches it via `mcp_call`, so each write is
approval-gated on `mcp:vault:<tool>` and event-logged. Vault semantics are not
re-implemented — the server calls `services/memory.py` (scrubbing, size bound,
frontmatter) and `services/memory_hooks.py` (`recall_recent`). The dedicated
Bob Agent record (`<vault>/records/bob-agent.md`, `type: agent-record`) is
updated in place: `events` counter + `updated` stamp advance and one entry is
appended, with the activity log capped at `MAX_RECORD_EVENTS`. Writes are
atomic (temp file + `os.replace`) and every failure is returned as an MCP
`isError` result — a broken vault can never crash the transport loop.

### 8.7 OpenConnector (SaaS connector gateway)

`services/openconnector.py` — self-hosted Pipedream/Composio alternative
(`ghcr.io/oomol-lab/open-connector`, port 3000 via `docker compose up openconnector`;
1,000+ providers, 10,000+ Actions). Two integration surfaces, both configured via
`OPENCONNECTOR_BASE_URL` (unset ⇒ tools stay **hidden**, no stubs):

1. **HTTP Runtime API** — `execute_action` = `POST /v1/actions/:actionId
   {input, connectionName?}`; `list_actions` = `GET /v1/actions[?service=]`;
   `get_action_guide` = `GET /api/actions/:id/agent.md`; `health_check`. Credentials
   stay inside the gateway; the response carries `data` + `meta.executionId` (audit).
   Headers: `Authorization: Bearer <runtime_token>` + optional `x-oo-connector-alias`.
2. **Implicit MCP-over-HTTP** — `implicit_mcp_server()` returns an MCP_SERVERS-style
   dict for `POST /mcp` (Bearer runtime token), so every OpenConnector Action appears
   natively as an MCP tool via `mcp_list`/`mcp_call`. `OOMOL_CONNECT_ALLOWED_ACTIONS`
   constrains what the gateway may run.

### 8.8 Permission gate & approvals

`services/permissions.py` — centralized, in-memory, **default-deny**. No component may
bypass it. Enums: `Risk(LOW|MEDIUM|HIGH|CRITICAL)`, `Policy(ALLOW_ONCE|
ALLOW_SESSION|ALLOW_WORKSPACE|ALLOW_ALWAYS|DENY)`, `Decision(PENDING|APPROVED|
DENIED|EXPIRED)`.

Request → `PermissionGate.request(ApprovalRequest)` returns an `ApprovalRecord`
(`new_approval_id()` = `approval_…`):
- **Dangerous scopes** (`host:filesystem`, `host:shell`, `host:credentials`,
  `host:users`, `host:firewall`, `host:bootloader`, `host:security_software`,
  `browser:transact`, `credential:transmit`, `autopilot:input`, `a2a:delegate`)
  are **always default-deny** and can never be `ALLOW_ALWAYS`.
- Every request yields a record with an expiry by TTL: `LOW=60min`,
  `MEDIUM=30min`, `HIGH=15min`, `CRITICAL=5min`.
- `check()` walks grants (most recent first); consumes `ALLOW_ONCE`; matches
  `ALLOW_WORKSPACE`/`ALLOW_SESSION` scope to the request; **expired ⇒ fail-closed**.
- `sweep_expired()` reaps pending records past expiry.

The API layer persists grants (`POST /api/v1/approvals`, `…/decision`, `…/sweep`).
`tools` tool failures raise `NeedsApprovalError` carrying the persisted `approval_id`;
the user resolves via `/approve` (chat), the dashboard Approvals tab, or
`agentctl approvals decide`; the task is then retried. Approval payloads carry
`sensitivity=SENSITIVE`.

### 8.9 Memory + Obsidian vault

`services/memory.py` + `services/memory_hooks.py`. Two components:

- **`ObsidianVaultWriter`** — writes Markdown with YAML frontmatter + `[[wiki-links]]`
  into `VAULT_PATH/memory/{system,user,task,workspace}/`, each note attributed with
  source/task/session/agent/timestamp.
- **`MemoryStore`** — layered (SYSTEM|USER|TASK|WORKSPACE) in-process index with
  optional embeddings. When no real embedding provider is wired, retrieval falls back to
  `HashEmbedding` (deterministic lexical hashing) — weaker, but **documented and honest,
  never fake results**. `_cosine` ranking.

`memory_hooks.remember_outcome()` auto-writes a scrubbed task-outcome note after each
successful task (never raises — memory must not break execution); `recall_recent()`
does keyword-ranked recent-vault retrieval injected into ReAct prompts
(`memory_recall_top_k`, default 3). The chat exposes `/memory <fact>` and
`/recall <query>`. **Secrets never reach the vault**: `scrub_text()` redacts key-shaped
secret values (OpenAI/GitHub/Slack/AWS/JWT/private-key patterns) before persistence.

### 8.10 Skills

`services/skills.py` + `api/v1/skills.py`. A skill is `skills/<name>/SKILL.md` = YAML
frontmatter (`name, version, description, enabled, agents, config, author`) + a
Markdown instruction body that gets injected into an agent's prompt.

**Lifecycle:**
- Ship in `backend/skills/` (builtin: `web-research`, `document-craft`, `qa-assist`).
- Add via folder drop, `agentctl skills add <path|.md-url|git-url>`, or
  `POST /api/v1/skills[/import]` (local path / `.md` URL / `git clone --depth 1`).
  **Agents** use the same path with `author="agent:<type>"`.
- Enable toggles persist in `skills/.state.json`; per-user values in
  `skills/<name>/config.local.yaml` — **`SKILL.md` itself is never rewritten** by tooling.
- `SkillManager.compose(agent_type=, skills=[])` renders the `<skills>` prompt block;
  `ModelRouter.invoke` calls it automatically. `discover()`/`enabled()`/
  `get()`/`set_enabled()`/`set_config()`/`create_skill()`/`delete_skill()`.
- Invalid skills are reported in `last_errors`, never fatal. `git` required only for
  git-URL imports.

### 8.11 Docker sandbox

`services/sandbox.py`. `DockerSandbox` wraps the `docker` SDK; `DockerSandbox` raises
`SandboxUnavailableError` (→ API 503) when the daemon is down or the SDK missing — it
**never fakes isolation**.

- Base image `python:3.12-slim`; QA image `agent-system/qa-sandbox:latest` (Phase 13).
- `_limits()` enforces `MAX_CONTAINER_MEMORY_MB` (2048), `MAX_CONTAINER_CPU` (2.0 ⇒
  `nano_cpus=2e9`), `pids_limit=128` — both pulled from `Settings`.
- `network_disabled=True` by default; `network=True` re-enables it per call
  (`POST /workspaces/{id}/exec {network:true}`).
- Workspace bind-mounted at `/ws` (rw); container runs as the invoking UID/GID so
  files stay host-owned. `MAX_OUTPUT_BYTES=1_000_000` truncates logs; `timeout` defaults
  to `max_execution_time_seconds`. Container is force-removed in `finally`.
- `quote_args()` shell-quotes agent-supplied words so they can't break quoting.

### 8.12 Workspaces + templates

`services/workspaces.py`. Workspaces are plain on-disk directories; path ops are
**traversal-safe** (`_resolve_safe` resolves and asserts the result stays inside the
root, else `WorkspaceError`). `MAX_FILE_BYTES=10 MB`.

- `WorkspaceManager`: `create`, `path`, `write_file`, `read_file`, `tree` (excludes
  `.git/` internals — keeps HEAD), `fingerprint` (SHA-256 of sorted relpath+content,
  used by replay safety checks), `delete`.
- `TemplateManager.snapshot()` tars a workspace into a `.tar.gz` **scanning for and
  excluding secrets** (`is_secret_path`); `.git/` internals dropped except `HEAD`.
  `clone()` extracts safely (rejects absolute/`...` paths) and re-scans secrets on
  restore (defense in depth).

Workspace API (`router.py`): `POST /workspaces`, `GET /workspaces`, `GET
/workspaces/{id}/tree`, `POST /workspaces/{id}/file` (base64), `GET
/workspaces/{id}/file` (read raw), `GET /workspaces/{id}/fingerprint`,
`POST /workspaces/{id}/exec`, `DELETE /workspaces/{id}`.

### 8.13 Behavior recording + replay

`services/recording.py`. `BehaviorRecorder` wraps agent I/O and appends one JSON line
per action to `recordings/<id>.jsonl` (cap `MAX_STEPS=10_000`); secret-shaped values
are scrubbed at record time via `redact_dict`. `ReplayContext` is the **environment
fingerprint**: `workspace_fingerprint`, `os_platform`, `deps_fingerprint`,
`agent_version`, `model_config`, `recipe_version`, `permissions`.

`ReplayService.replay(recording_id, mode, context, approval_id)` implements three safe
modes:
- **INSPECT** — read-only walk. Always allowed.
- **SIMULATE** — dry-run handlers when registered, else replays from the log; fingerprint
  comparison reported, not enforced. No approval required.
- **APPROVED_REEXECUTE** — guarded path: requires **both** a fingerprint match against the
  current environment and a fresh approval. Fingerprint mismatch blocks (with the differing
  keys listed); no approval at all always blocks. Side effects are counted.

API: `GET /recordings`, `POST /recordings/{id}/replay {mode, context, approval_id}`.

### 8.14 Error recovery

`services/recovery.py` — Classify → plan → execute → learn.

- **`ErrorIntrospector.classify(error)`** — regex-based into `ERROR_CLASSES`
  (network, timeout, rate_limit, validation, permission, dependency, syntax,
  test_failure, resource_limit, provider_failure, unknown). `NON_RETRYABLE =
  {permission, validation}`.
- **`RecoveryPlanner.plan(classification, attempt, max_retries, last_error, patterns)`**
  — non-retryable ⇒ `escalate`; `attempt >= max_retries` ⇒ `escalate`; **repeated
  identical failure** (`last_error == classification.original`) ⇒ escalate (no retry
  storm); otherwise `retry` with class-aware adjusted params + exponential backoff
  (`BASE_BACKOFF=1s`, `MAX=60s`, x4 on rate_limit). Param hints: timeout ⇒
  `timeout_seconds=60`, network ⇒ `reconnect=True`, provider_failure ⇒
  `fallback_model=True`.
- **`RecoveryExecutor.execute(handler, plan, payload)`** — real `time.sleep` backoff
  (tests cap it), invokes the handler with adjusted params.
- **`PatternLearner`** — records `(error_class, agent_type)` success rates
  (`recovery_success_rate`), emits `preventive_actions()` suggestions for classes seen
  ≥2× (e.g. timeout ⇒ raise default timeout; rate_limit ⇒ add throttling).

Orchestrator's `recover_orphans()` provides the lease-level recovery
(RECOVERING→QUEUED while `attempt < 3`, else FAILED).

### 8.15 Cost accounting + budget monitor

`services/model_router.py:BudgetMonitor`. `set_budget(scope, limit_usd)` +
`record(scope, cost_usd)` returns **alert levels fired (50/75/90/100%, each once)**.
`spent()`/`remaining()`. The `DailyBudget` model (`cost_budget` table, column
`period`) + `DAILY_BUDGET_USD` (default 10.0) gate spend. Every `ModelCall`/`invoke`
fires a `cost.recorded` event with `model_call_id/cost_usd/model_id` — cost is
**derived from real pricing** (free-tier models at $0.00, billable ones flagged
`cost_is_estimated`).

### 8.16 Task batching

`services/batching.py:TaskBatcher`. `create_batch(factory, session_id, task_ids,
batch_type)` requires ≥2 tasks, shared agent type + task type, no inter-member deps,
all in PENDING/QUEUED. Incompatible batches raise `BatchError` (409 on conflict). Members
keep their own task lifecycles + events; partial failure isolates to the failing member.
`cancel_batch()` cancels pending/queued members (running ones finish).
`record_batch_result()` computes `speedup_factor = sequential_estimate / batch_seconds`
and closes the batch. `batch_status()` returns per-state member counts.
API: `POST /batches`, `POST /batches/{id}/cancel`, `GET /batches/{id}`.

### 8.17 Recipes

`services/recipes.py:RecipeEngine`. A recipe = versioned task DAG (`task_dag_json`) +
`parameters_json` + `tags_json`. **DAG validation** (`_validate_dag`): non-empty
`tasks` list, unique `key`s, each has `task_type`/`title`, deps reference known keys,
**DFS cycle detection**. Parameter substitution via `{{name}}` regex
(`_substitute_deep`). Execution rides the canonical task pipeline — recipes create real
`Task` rows through the `Supervisor`, never bypassing permissions or the event bus — so
each member keeps its own lifecycle/events. `recipe.started/completed/failed` events +
`recipe:` title prefix on tasks for the cancel run. APIs: `POST /recipes` (create,
validates), `GET /recipes`, `GET /recipes/{id}`, `POST /recipes/{id}/execute`
(checks `_missing_params`), `POST /recipes/{id}/cancel`.

### 8.18 Insights

`services/insights.py:InsightGenerator`. Derives **only** from canonical events
(`EventRow`) — never from private tables, never fabricated (zero events ⇒ honest
empty insight). Types: `daily`, `weekly`, `anomaly` (validated).
`_summarize()` aggregates event counts, task completed/failed, approvals, cost ⇒
findings + HTML content. `_generate_anomaly()` flags failure-rate >30% (suggests
review recovery patterns), ≥5 recovery events (inspect leases/worker), ≥3 expired
approvals (review queue) — each with a `suggested_action`. All content passes
`scrub_text()` before persistence. APIs: `POST /insights/generate`, `GET
/insights?include_archived`, `POST /insights/{id}/archive`.

### 8.19 Personality + feedback loop

`services/personality.py:PersonalityManager`. Versioned per-agent
tone/verbosity(1–10)/reasoning_style/system_prompt_override in `agent_personalities`,
feedback in `feedback_log` (rating 1–5). **Security-bounded by construction**:
`_ADJUSTABLE = (tone, verbosity, reasoning_style, system_prompt_override)` only —
`update()` rejects any other key (`PersonalityError`); the feedback loop never touches
safety/permission/budget/limit settings. `learn_from_feedback()` runs after ≥10
(`LEARNING_THRESHOLD`) ratings: low avg (<2.5) ⇒ lower verbosity by 2, reasoning_style
`concise`; high avg (>4.5) ⇒ +1 verbosity. APIs: `GET /personality/{id}`,
`PUT /personality/{id}`, `POST /personality/{id}/feedback`,
`POST /personality/{id}/learn`.

### 8.20 Autopilot (desktop automation)

`services/autopilot.py` — the most dangerous capability, gated hardest:

- **Off by default** — a no-op unless `enable()`d; disabled service refuses every run
  and records the refusal. Instantiated in `main.py` with `enabled=False`.
- **Own permission boundary** — every input action (`click`, `type_text`, `key_press`,
  `scroll`, `move_mouse`) needs a **fresh, unexpired, APPROVED** record bound to exactly
  `autopilot:<action>`; expired/pending/mismatched ⇒ `denied_gate`.
- **Hard default-deny** — `FORBIDDEN_ACTIONS` (`pay`, `purchase`, `checkout`,
  `send_email`, `delete_account`, `change_password`, `grant_permission`) can never be
  approved, regardless of approvals. Unknown actions ⇒ forbidden.
- **Hard caps** — `MAX_ACTIONS_PER_RUN=50`, `MAX_RUN_SECONDS=300`.
- **Kill switch** — `kill()` is idempotent/immediate/sticky; only `reset()`
  re-arms. `audit()` returns every executed/denied/refused/killed action.
- The action executor is injected at composition (`executor` callable) — this module
  never imports `pyautogui`/`pynput` directly, keeping the risky surface behind one
  seam. APIs: `GET /autopilot/status`, `POST /autopilot/kill`, `POST /autopilot/reset`.

### 8.21 Telegram gateway

`services/telegram.py` + `api/v1/telegram.py`. Optional gateway; boots **only** when
`TELEGRAM_BOT_TOKEN` is set, and a broken config never takes down the API
(`lifespan` swallows the start error). Two runs:

- **Long-polling** (default, dev) — no `TELEGRAM_WEBHOOK_SECRET`.
- **Webhook** (production) — set `TELEGRAM_WEBHOOK_SECRET`; point Telegram at
  `POST /api/v1/telegram/webhook` with header `X-Telegram-Webhook-Secret` (HMAC
  compared, constant-time).

**Auth/allowlist**: only allowlisted `TELEGRAM_ALLOWED_CHAT_IDS` (csv, parsed to
`set[int]`) get responses — enforced per-message. Bot replies are tied to the same
`/api/v1` contracts as the dashboard (so Telegram users issue goals that become
sessions). APIs: `POST /telegram/webhook` (unauthenticated, header-gated),
`GET /telegram/status` (authenticated).

### 8.22 Auth

`services/auth.py:Authenticator`. Localhost-first but still authenticated.
`AGENT_BOOTSTRAP_SECRET` → `POST /api/v1/auth/token {session_secret}` → HMAC-SHA256
hashed Bearer token (stored hashed, compared with `hmac.compare_digest`). The CLI
auto-mints a token from the local secret (`--token`/`AGENTCTL_TOKEN` override); the
dashboard uses a signed `agent_session` cookie fallback on localhost. `Authenticator`
is in-memory per-process (tokens minted server-side); `require_auth` (FastAPI
dependency in `api/deps.py`) raises 401 on missing/invalid tokens.

### 8.23 Secret redaction

`services/secrets.py` — defense in depth applied at every output boundary.

- **Key markers** (case-insensitive substring): `api_key`, `apikey`, `secret`,
  `password`, `token`, `authorization`, `cookie`, `private_key`, `credential`,
  `session_key`, `signing_key`, `database_url`, `connection_string`.
- **Value patterns** (regex-substituted to `[REDACTED]`): `sk-…` OpenAI keys,
  `AKIA…` AWS ids, `-----BEGIN … PRIVATE KEY-----`, JWTs, `ghp/…` GitHub tokens,
  `xox[baprs]-…` Slack tokens.
- **`SECRET_FILENAMES`** (`.env*`, `credentials.json`, `secrets.json`,
  `service-account.json`, `id_rsa`, `id_ed25519`, `.npmrc`, `.pypirc`, `.netrc`,
  `.git-credentials`, `.htpasswd`) and **`SECRET_DIRS`** (`.ssh`, `.gnupg`, `.aws`,
  `.kube`, `.docker`, `.config/gcloud`) → never copied into templates/snapshots.
- `is_secret_path()` guards file tools + template snapshot/clone; `redact_dict()`
  scrubs event payloads at `EventBus.emit()` (shallow + nested); `redact_value()` for
  free text. `settings list`/setup previews/logs all mask secrets unless
  `--show-secrets`.

### 8.24 Tool-fence injection filter

`services/agent_loop.py:sanitize_tool_result()`. Tool results (`web_fetch`,
`mcp_call`, …) flow back into the prompt as `<tool_result>` blocks. If such
content contained a live ` ```tool:shell …``` ` fence and the model echoed it,
the next loop iteration would re-parse it as a genuine tool call — a
prompt-injection vector via untrusted tool output. Every `tool_result` payload
is sanitized before re-entering the prompt (an invisible zero-width break is
inserted after any opening ` ```tool:` so `TOOL_FENCE_RE` no longer matches,
rendering unchanged). Only result content is sanitized — the model's own
outgoing fence parsing is untouched. Regression:
`tests/security/test_tool_fence_injection.py` (injected `shell` fence in a
`web_fetch` result is never executed; legitimate calls still parse).

### 8.25 Memory embeddings (hash default, local opt-in)

`services/memory.py`. `MemoryStore` retrieval ranks by `_cosine` over
embeddings. The zero-dependency default is unchanged: `HashEmbedding`
(deterministic lexical hashing, honest baseline). Opt-in semantic recall via
`LocalEmbeddingProvider` (`sentence-transformers` `all-MiniLM-L6-v2`,
CPU-friendly, no API key) behind `MEMORY_EMBEDDING_PROVIDER` (`hash` default |
`local` | anything else fails closed with `MemoryError`). The model package
lives in the `memory` extra (`pip install agent-system[memory]`); constructing
the provider without it raises with install instructions instead of silently
falling back. `embedding_provider_from_settings()` / `build_memory_store()`
compose the backend; the setup wizard offers the choice (derived catalog).
Tests: `tests/unit/test_memory_embeddings.py` (defaults untouched, unknown
provider fails closed, paraphrase fixture ranks first under `local` when the
extra is installed, skipped otherwise).

### 8.26 Context compaction

`services/agent_loop.py` (`estimate_tokens`, `run_tool_loop`). `tools_max_iters`
caps iterations, not context: accumulated `<tool_result>` content is
token-estimated with a documented chars/4 heuristic (budgeting only, never
billing). Past `CONTEXT_COMPACTION_THRESHOLD_PCT` (default 75%) of
`MAX_CONTEXT_TOKENS` (default 100_000) the oldest results are dropped in favour
of a one-line synthetic `<tool_result name="context-summary">` (newest 3 kept)
and a `context.compacted` event (`dropped_count`, `estimated_tokens_saved`)
fires — graceful degradation instead of a blown context window. Budgets resolve
from explicit args or `ctx.settings`; `react_agent` passes the settings values.
Tests: `tests/unit/test_context_compaction.py` (oversized chain compacts +
event; short loops never compact; settings resolution).

### 8.27 Provider circuit breaker

`services/model_router.py:ProviderCircuitBreaker` (per provider key). After
`CIRCUIT_BREAKER_THRESHOLD` (default 5) consecutive `model.failed` events the
circuit trips OPEN for `CIRCUIT_BREAKER_COOLDOWN_SECONDS` (default 60): calls
fail fast with `ProviderUnavailableError` (no network call, never a fabricated
response) while still recording an honest failed `ModelCall`. After cooldown
exactly one half-open probe goes through — success closes (resetting cooldown),
failure re-opens with doubled cooldown (cap 10 min). `model.circuit_opened` /
`model.circuit_closed` events fire. Wired into `ModelRouter.invoke` (and the
streaming path) before the adapter call; thresholds are constructor settings
fed by `providers.build_model_router`. Tests:
`tests/unit/test_circuit_breaker.py` (trip, fast-fail without network, probe
recovery, probe-failure redoubling, success reset, probe serialization).

### 8.28 Backups

`services/backup.py:BackupService`. Each `run()` writes
`backups/<id>/{agent_system.sqlite3, vault_recordings.tar.gz, manifest.json}`:
SQLite via the online backup API (WAL-safe, never a raw copy); vault +
recordings as tar reusing `secrets.is_secret_path` (same predicate as
`TemplateManager.snapshot()`, not a copy). Retention keeps the newest
`BACKUP_RETENTION_COUNT` (default 7). A nightly APScheduler job
(`BACKUP_SCHEDULE_CRON`, default `0 3 * * *`, `scheduler_enabled` gate) runs it
in-process; every run emits `backup.completed` / `backup.failed`. CLI:
`agentctl backup run|list|restore <id>`; `restore` requires `--confirm` and
refuses a live server (port probe on `api_port`) without `--force-live`.
Tests: `tests/unit/test_backup.py` (restorable SQLite opened + queried,
secrets excluded, pruning, confirm + live guards, round-trip).

### 8.29 Pluggable tools

`services/tool_plugins.py` + `services/tools.py:build_registry`. A plugin is a
folder `TOOLS_PLUGIN_DIR/<name>/` (`tools_plugin_dir`, default
`tools_plugins/`) with `tool.json` (description, parameters JSON schema, risk,
`handler: "file.py:func"`) + handler defining `handle(args) -> dict` — the
same folder-drop + `.state.json` toggle pattern as `SkillManager`
(`ToolPluginManager.discover/enabled/get/set_enabled/build_tools`; invalid
plugins land in `last_errors`, never fatal). Safety is identical to built-ins:
`execute` plugins are always approval-gated (same DB flow +
`NeedsApprovalError`) and always run inside `DockerSandbox` via a staged
runner (args JSON in, result JSON out) — `tools_shell_mode=local` does not
apply, sandbox-unavailable is an honest error, never a host fallback. Plugins
can never shadow built-ins; discovery re-reads every `build_registry` call so
toggles apply on the next task with no restart. Sample:
`backend/tools_plugins/_examples/text_stats/`. CLI parity:
`agentctl tools list|enable|disable`. Tests:
`tests/unit/test_tool_plugins.py` (read plugin callable from the ReAct loop,
execute forced through sandbox, approval demand, disable removes from
`prompt_block()`, validation, no shadow).

### 8.30 Token-level streaming

`services/providers.py` (`stream()` on OpenAI-compatible SSE `stream:true` +
Anthropic SSE; `(chunks, usage)` contract, usage empty when the provider omits
it), `services/model_router.py:invoke_streaming` (new path alongside
`invoke`): per-chunk `model.token` events (visibility=user) as deltas arrive,
then exactly one `model.completed` with full usage/cost — accounting identical
to `invoke` (shared `_record`). Non-streaming adapters fall back to one
`invoke` + a single full-output `model.token` (same event shape); echo streams
word-by-word so offline mode works. Breaker, skills/soul composition,
`ModelCall`, and `cost.recorded` behave identically on both paths. Consumers:
`react_agent` streams with `on_token` → bus; WS/SSE fan out unchanged (no
transport whitelist); `agentctl chat` renders deltas inline; dashboard
`app/chat/` accumulates `model.token` into a live bubble finalized by
`model.completed`. Tests: `tests/unit/test_streaming.py` (incremental tokens,
identical accounting, single `ModelCall`, fallback shape, mid-stream failure).

### 8.31 Metrics & tracing

`infra/telemetry.py` + `OTEL_EXPORTER_OTLP_ENDPOINT` (unset ⇒ fully disabled:
no OTel imports, zero new required deps, only cheap in-memory counters).
`setup_telemetry(settings, app)` (called in `api/main.py:lifespan`, never
fatal) wires OTLP trace/metric export plus FastAPI + SQLAlchemy + httpx
auto-instrumentation when the endpoint is set (needs
`pip install agent-system[telemetry]`; missing extra ⇒ disabled with a
warning). Business metrics (in-memory always, OTel mirrors when enabled):
task duration by terminal state (worker success/fail), model latency by
provider (router `_record`), tool calls by risk tier (agent loop), approval
decision latency by scope (`decide_approval`), cost per session. `Metrics`
never raises; `snapshot()` exposes aggregates for tests/debug. Observability
stack documented in `docs/OPERATIONS.md` (optional compose profile:
collector + Prometheus + Grafana). Tests: `tests/unit/test_telemetry.py`
(disabled default imports nothing, missing-extra warning, wiring records,
OTel smoke skipped without the extra).

### 8.32 Agent-to-agent handoff

`services/a2a.py` + `api/v1/a2a.py`. Minimal outbound delegation, off by
default (`A2A_ENABLED=false`): `POST /api/v1/a2a/delegate` (auth) signs the
task envelope (HMAC-SHA256 over canonical JSON, keyed by
`API_SESSION_SECRET`; the external agent shares the secret out-of-band) and
POSTs it with a `callback_url`; the agent's signed result returns to
`POST /api/v1/a2a/callback` (unauthenticated — the HMAC IS the auth, Telegram-
webhook pattern; 10-minute freshness window) which verifies, flips the task
row RUNNING→SUCCEEDED with `result_json`, and emits `a2a.result` +
`task.completed`. Gating mirrors autopilot: `a2a:delegate` is a default-deny
scope in `DANGEROUS_SCOPES` (never `ALLOW_ALWAYS`); each delegation needs a
fresh, unexpired, APPROVED gate record bound to exactly
`a2a:delegate:<host>:<task_id>` (Risk HIGH), else `A2ANeedsApprovalError`
(HTTP 409 with `approval_id`) is raised and no network call is made. Delivery
failures raise `A2AError` (+ `a2a.failed`) — never fabricated results.
`GET /api/v1/a2a/delegations` lists known delegations. Tests:
`tests/unit/test_a2a.py` (off by default, bare-scope deny, approval-first with
zero network calls, full round-trip against a fake external HTTP agent with
task completion + events, tamper/unknown/non-HTTP rejections, endpoint 503/401).

## 9. API reference (REST `/api/v1`)

Base path `/api/v1`. All **authenticated** routes require `Authorization: Bearer <token>`
(obtained via `POST /auth/token`). The Next.js dashboard proxies same-origin
(`/api/v1/**` → FastAPI via `AGENT_SYSTEM_API_URL`). OpenAPI auto-generated at
`/docs` (Swagger) and `/redoc`. Exit-code-stable wrapper in the CLI (`api/deps.py`,
`services/auth.py`). Two routers hold endpoints: `api/v1/router.py` (core) and
`api/v1/features.py` (+ `skills.py`, `realtime.py`, `telegram.py`).

### 9.1 Health & auth

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/v1/health` | none | `{"status":"ok"}` |
| GET | `/api/v1/ready` | none | `{"status","checks":{"database":bool}}` |
| GET | `/api/v1/ready-dependency-check` | Auth | dependency wiring probe (hidden) |
| POST | `/api/v1/auth/token` | none | `{session_secret}` → `{token}` (HMAC compare to `AGENT_BOOTSTRAP_SECRET`) |

### 9.2 Sessions

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/sessions` | Auth | create session (`{goal}`) → `{id,goal,status}` + `session.created` |
| GET | `/sessions` | Auth | list sessions (newest first) |
| GET | `/sessions/{id}` | Auth | get one session |
| GET | `/sessions/{id}/tasks` | Auth | tasks for a session |
| POST | `/sessions/{id}/complete` | Auth | mark session completed (`session.completed`) |

### 9.3 Tasks (DAG nodes)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/tasks` | Auth | list (filters: `session_id`, `state`, `limit≤1000`) |
| GET | `/tasks/{id}` | Auth | get one task (`TaskOut`) |
| GET | `/tasks/transitions` | Auth | allowed task-state transitions |
| POST | `/tasks/{id}/transition` | Auth | explicit **validated** transition (invalid ⇒ 409) |
| POST | `/tasks/{id}/retry` | Auth | 202 → requeue (PENDING/QUEUED/RECOVERING/FAILED only) |
| POST | `/tasks/{id}/cancel` | Auth | cancel (QUEUED→CANCELLED, RUNNING→cooperative flag) |
| POST | `/tasks/run` | Auth | enqueue a `QUEUED` task onto the RQ worker (`{task_id}`) |

`TaskOut` = `{id, session_id, task_type, title, state, agent_type, depends_on,
attempt, last_error, started_at, completed_at}`.

### 9.4 Approvals

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/approvals` | Auth | request approval (SENSITIVE) → 202 |
| GET | `/approvals` | Auth | list (`pending_only=true` default; `?pending_only=false`) |
| GET | `/approvals/{id}` | Auth | get an approval record |
| POST | `/approvals/{id}/decision` | Auth | `{approve, policy, reason}` |
| POST | `/approvals/sweep` | Auth | expire past TTL pending approvals |

`Risk ∈ LOW|MEDIUM|HIGH|CRITICAL`; `Policy ∈ ALLOW_ONCE|ALLOW_SESSION|
ALLOW_WORKSPACE|ALLOW_ALWAYS|DENY`; `Decision ∈ PENDING|APPROVED|DENIED|EXPIRED`.

### 9.5 Workspaces

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/workspaces` | Auth | create (`{name}`) → `{id,name,status}` |
| GET | `/workspaces` | Auth | list |
| GET | `/workspaces/{id}` | Auth | get one |
| GET | `/workspaces/{id}/tree` | Auth | file tree |
| GET | `/workspaces/{id}/fingerprint` | Auth | SHA-256 content fingerprint |
| POST | `/workspaces/{id}/file` | Auth | write file (base64 content; traversal-guarded) |
| GET | `/workspaces/{id}/file` | Auth | read file (`?path=`) |
| POST | `/workspaces/{id}/exec` | Auth | run command in Docker sandbox (`{command, timeout_seconds≤1800, network}`) |
| DELETE | `/workspaces/{id}` | Auth | delete |

### 9.6 Artifacts & events

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/artifacts` | Auth | list (`?task_id=`) |
| GET | `/artifacts/{id}` | Auth | get one |
| GET | `/events` | Auth | list (`?after_sequence=&type=&session_id=&limit≤1000`) — resume-from-sequence |

### 9.7 Recordings + replay

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/recordings` | Auth | list recordings |
| POST | `/recordings/{id}/replay` | Auth | `{mode:INSPECT|SIMULATE|APPROVED_REEXECUTE, context, approval_id}` → full result incl. steps/side_effects |

### 9.8 Task batching

| Method | Path | Auth |
|---|---|---|
| POST | `/batches` `{session_id, task_ids≥2, batch_type}` | Auth |
| POST | `/batches/{id}/cancel` | Auth |
| GET | `/batches/{id}` | Auth |

### 9.9 Recipes

| Method | Path | Auth |
|---|---|---|
| POST | `/recipes` `{name, description, task_dag, parameters, tags}` | Auth |
| GET | `/recipes` | Auth |
| GET | `/recipes/{id}` | Auth |
| POST | `/recipes/{id}/execute` `{params}` | Auth |
| POST | `/recipes/{id}/cancel` | Auth |

### 9.10 Personality + feedback

| Method | Path | Auth |
|---|---|---|
| GET | `/personality/{agent_id}` | Auth |
| PUT | `/personality/{agent_id}` `{tone?,verbosity?,reasoning_style?,system_prompt_override?}` | Auth |
| POST | `/personality/{agent_id}/feedback` `{rating∈1..5, comment?, session_id?}` | Auth |
| POST | `/personality/{agent_id}/learn` | Auth (≥10 ratings → adjust) |

### 9.11 Insights

| Method | Path | Auth |
|---|---|---|
| POST | `/insights/generate` `{insight_type:daily|weekly|anomaly}` | Auth |
| GET | `/insights` `?include_archived=false` | Auth |
| POST | `/insights/{id}/archive` | Auth |

### 9.12 Cost / model routing / QA

| Method | Path | Auth |
|---|---|---|
| GET | `/model-calls` `?task_id=` | Auth |
| GET | `/model-routing/providers` | Auth (catalog + pricing + router state) |
| POST | `/model-routing/test` `{provider, model?, prompt=}` | Auth |
| GET | `/qa-reports` `?limit≤200` | Auth |

### 9.13 Schedule (APScheduler)

| Method | Path | Auth |
|---|---|---|
| POST | `/schedule` `{name, kind:cron|interval|date|webhook, schedule, payload}` | Auth |
| GET | `/schedule` | Auth |
| DELETE | `/schedule/{id}` | Auth |

### 9.14 Autopilot (off by default)

| Method | Path | Auth |
|---|---|---|
| GET | `/autopilot/status` | Auth |
| POST | `/autopilot/kill` | Auth |
| POST | `/autopilot/reset` | Auth |

### 9.15 Skills (`api/v1/skills.py`, prefix `/api/v1/skills`)

| Method | Path | Auth |
|---|---|---|
| GET | `/skills` `?agent_type=` (list summaries + `errors`) | Auth |
| GET | `/skills/{name}` (full: instructions + config) | Auth |
| POST | `/skills` `{name, description, instructions, agents?, config?, author?}` | Auth (+ `skill.created`) |
| PATCH | `/skills/{name}` `{enabled?, config?}` | Auth (+ `skill.updated`) |
| DELETE | `/skills/{name}` | Auth (+ `skill.deleted`) |
| POST | `/skills/import` `{source, overwrite?}` (path / .md URL / git URL) | Auth |

### 9.16 Telegram (`api/v1/telegram.py`, prefix `/api/v1/telegram`)

| Method | Path | Auth |
|---|---|---|
| POST | `/telegram/webhook` (header `X-Telegram-Webhook-Secret`) | none (header-gated) |
| GET | `/telegram/status` | Auth |

### 9.17 Realtime (`api/v1/realtime.py`, prefix `/api/v1`)

| Method | Path | Auth |
|---|---|---|
| WS | `/ws/events?after_sequence=N` | handshake-authed |
| GET | `/events/stream?after_sequence=&max_events=` (SSE) | handled per-route |
| GET | `/events/latest-sequence` | Auth |

## 10. Realtime transport (WS + SSE)

`api/v1/realtime.py`. The EventBus persists to SQLite; realtime fans out the *same*
envelope over two transports so the dashboard/REPL and Telegram all see a single
source of truth. Key guarantees:

- **Sequence numbers** are monotonic per bus (`_sequence`, assigned on `emit`).
  Clients resume with `after_sequence` — reconnects never miss events.
- **Per-session broadcast** — the dashboard tails `session.created`, `session.completed`,
  `task.*`, `model.*`, `tool.*`, `approval.*`, `cost.recorded`, `insight.generated`,
  `recovery.*`, `recipe.*`, `batch.*`, `qa.*`.
- **Visibility & sensitivity** — `EventVisibility.INTERNAL` events are stripped from
  non-admin channels; `EventSensitivity.SENSITIVE` payloads (e.g. approvals) are
  redacted by `secrets.redact_dict` before fan-out.
- Same-origin CORS is locked in production (dev allows `localhost:*`).
- `GET /api/v1/events/latest-sequence` gives a cursor for SSE resumption.

WS: `ws(s)://<host>:<port>/api/v1/ws/events?after_sequence=N`
SSE: `GET /api/v1/events/stream?after_sequence=&max_events=`.

## 11. CLI — `agentctl`

`cli/` is the impl; `cli/agentctl.py` is the thin launcher
(`python3 cli/agentctl.py <command>` ⇒ `agent_system.cli.main`). Built on Typer with
Rich panels/tables and **exit-code-stable** semantics (0 success, 2 usage error,
4 config/auth error, 6 partial success, 1 unexpected).

**Commands** (categories):

- *Sessions:* `sessions create|list|get|complete`
- *Tasks:* `tasks list|get|retry|cancel|run|transitions`
- *Chat (REPL):* `chat` — interactive ReAct session with slash commands:
  `/help`, `/model set <provider/model>`, `/tools`, `/shell sandbox|local|off`,
  `/approve <id>`, `/deny <id>`, `/sessions`, `/tasks`, `/recordings`, `/replay`,
  `/recipes`, `/schedule`, `/soul`, `/settings`, `/memory`, `/recall`, `/cost`,
  `/agents`, `/mcp`, `/openconnector`, `/autopilot`, `/whoami`, `/events` (live tail),
  `/export`, `/exit`. `/model set` swaps providers live.
- *Skills:* `skills list|add|enable|disable|config|import|delete`
- *Workers:* `worker start|stop|restart|status|ping|orphans|recover`
- *Approvals:* `approvals list|get|decide|sweep`
- *Workspaces:* `workspaces create|list|get|file|exec|delete`
- *Recordings:* `recordings list|replay`
- *Recipes:* `recipes list|run|cancel`
- *Batches:* `batches create|list|get|cancel`
- *Insights:* `insights generate|list|archive`
- *Personality:* `personality get|update|feedback|learn`
- *Model routing:* `model-routing providers|test <provider>`
- *Settings:* `settings list|get|set|unset|check|wizard|doctor`
- *System:* `doctor` (env probe via `cli/sysdetect.py` → `SystemReport`),
  `soul set|show` (edit/view `SOUL.md`), `setup` (wizard),
  `bootstrap` (one-shot install: env check → install deps → Redis → DB init →
  first admin token → smoke test).
- *Secrets:* `secrets scan <path>` (run `is_secret_path` over a file tree for review).

`make` shortcuts: `setup`, `start` (server+worker), `stop`, `restart`, `test`,
`lint`, `typecheck`, `check`, `up`/`down` (Docker Redis/OpenConnector), `format`,
`clean`.

## 12. Frontend — Next.js dashboard

`web/` (Next.js 16, React 19, TypeScript 5.5, Tailwind). Pages route from
`src/app/`; every backend call goes through **one** typed client `src/lib/api.ts`
(same base URL / auth header as the CLI) so web and CLI never drift.
`src/app/api/v1/[...path]/route.ts` proxies `/api/v1/**` → FastAPI
(`AGENT_SYSTEM_API_URL`) and tees `Authorization` headers.

**Sidebar navigation** (`components/Sidebar.tsx` → `NAV`): Dashboard, Chat, Kanban,
Workspace, Vault, Outputs, Schedule, Approvals (live badge, polls pending count
every 5s), Templates, Cost, Recipes, Reasoning, Insights, Settings, Audit Log.

**Key pages / client calls:**
- `app/chat/` → `/sessions`, live tool-stream over WS, `/replay`, `/approvals`.
- `app/kanban/` → `/tasks` (drag to reprioritize → `tasks/{id}/transition`).
- `app/workspace/` → `/workspaces/{id}/file`, `…/exec` (SandboxedShell, caps shown).
- `app/vault/` → vault notes; `app/outputs/` → `artifacts` + downloads.
- `app/schedule/` → `/schedule` (CRUD), `app/recipes/` → `/recipes` (run/cancel).
- `app/cost/` → `/model-calling` (spend vs `daily_budget_usd`), `app/insights/`.
- `app/approvals/` → `/approvals` (+ `/decision`), `app/reasoning/` → per-step tool calls.
- `app/settings/` → `/settings` (providers/skills/identity/mcp/oc), `app/audit/` →
  `/events` (filter by session/task/type/sequence).

`web/smoke-test.mjs` is a headless Puppeteer smoke test run by `make check`.
## 13. Operational runbook

**Start everything:** `make start` (Uvicorn on the API port from `api_port`, plus an RQ
worker). `make up` boots the optional Docker services (Redis + OpenConnector).
`make stop` tears them down; `make doctor` / `agentctl doctor` prints a `SystemReport`
(tools found, ports, Redis reachability, Python version).

**Local dev loop:** `cd backend && uv run python -m agent_system.main --reload`
(server) and in another shell `uv run python -m agent_system.worker` (worker). The REPL
`agentctl chat` proxies to the API. With **no** providers/Redis/OpenConnector
configured, Bob still boots in **offline echo mode** — the full ReAct loop runs,
records, replays, but never hits the network.

**Migrations:** `make migrate` (autogenerate + stamp) — never edit SQLite by hand.
Two baseline revisions ship: `94be8eadb99f_phase1_canonical_schema.py` and
`7c1a2d9e4f50_phase2_19_additions.py`. `DATABASE_URL` switches the store (Postgres
works) without code changes.

**Recovery from crashes:** the worker heartbeat thread refreshes
`agent_leases.lease_expires_at` every 15s; if a worker dies mid-run the lease
expires and `Orchestrator.recover_orphans()` reclaims the run (RECOVERING→QUEUED,
re-attempted while `attempt < 3`, else FAILED). `agentctl worker orphans|recover`
inspect/reclaim these manually. Idempotency on `/tasks` + `/sessions`
(`idempotency_keys` table) means a client retry never double-creates a task.

**Secrets hygiene:** never commit `.env.local`; `secrets scan <path>` reviews a tree
before templating a workspace snapshot. Templates always re-scan on clone.
`--show-secrets` is required to ever print a secret value (default: masked in
settings list / setup / logs).

**Performance:** SQLite in WAL mode + busy_timeout=5000 keeps concurrent worker
writes safe; heavy per-task writes use `session_scope` (transactional commit/
rollback/close). RQ is coordination only — the DB is authoritative and always
re-derivable by the worker.

## 14. Testing strategy

**492 tests** (484 passed, 8 skipped — dependency-gated: `sentence-transformers`
and the `telemetry` extra), all green under `make test` (pytest). Tests target
`backend/tests/{unit, contract, integration, recovery, security}` and run **offline
first** — they never require real LLM APIs or a live Redis (an `EchoProvider` /
in-process orchestrator + stub providers cover the loop). The suite is the contract
for *what the code does*; this doc must not drift from it.

- **unit/** — pure-logic, no I/O: `domain/tasks.py` state machine
  (`test_validate_transition_*`), `domain/ids.py`, recipes DAG validator
  (`test_validate_dag_*`), batching compatibility (`test_incompatible_*`), personality
  feedback (`test_learn_from_feedback_*`), cost budgeting thresholds, secret redaction
  patterns, sandbox `quote_args`, memory scrubbing.
- **contract/** — provider adapters against a local `httpx.MockTransport`: each
  `PROVIDERS` key (`test_providers_catalog_test.py`) plus `test_provider_adapter.py`
  verify request shape + response parsing + cost estimation without network.
- **integration/** — end-to-end with a real FastAPI `TestClient` + a real SQLite
  DB: `test_sessions_tasks.py` (session→tasks→retry→cancel), `test_approvals.py`,
  `test_workspaces.py`, `test_skills.py`, `test_recipes.py`, `test_model_router.py`
  (`skills_used`/`soul_used` recorded, echo costs $0), `test_realtime.py` (WS replay
  from `after_sequence`), `test_openconnector.py`, `test_mcp.py`, `test_vault.py`.
- **recovery/** — crash & retry contracts: `test_recovery.py` (the 10 scenarios:
  1. transient network error with `simulate_seconds=0` resolves immediately;
  2. task-level retry increments `attempt` exactly; 3. max-retry exhaustion sets FAILED
  + `reason="max_retry_exhausted"`; 4. repeated identical errors stop retrying;
  5. non-retryable errors escalate immediately; 6. `simulate_success_on_retry` flips
  FAIL→SUCCEEDED; 7. escalation path keeps state SUCCEEDED; 8. escalation to FAILED
  marks `reason="escalated"`; 9. escalation-then-retry escalates on a retryable second
  error; 10. escalation to SUCCEEDED when the retried attempt succeeds.),
  `test_recovery_patterns.py`, `test_worker_crash_*` (lease expiry reclaims orphans),
  `test_retry_budget.py`.
- **security/** — never-fabricated guarantees: `test_secrets.py` (key/value/filename
  markers, vault scrubbing), `test_sandbox_isolation.py`, `test_template_secrets.py`,
  `test_autopilot_default_deny.py` (every `FORBIDDEN_ACTIONS` is blocked + payment/
  credential/account/permission actions are never executable regardless of approval),
  `test_insights_no_fabrication.py` (empty DB ⇒ honest empty insight),
  `test_model_echo_no_fabrication.py`, `test_tool_fence_injection.py` (P0: injected
  tool fences in untrusted tool output are sanitized, never executed).
- **hardening pass** — `test_memory_embeddings.py` (hash default untouched, local
  opt-in recall), `test_context_compaction.py` (budget + `context.compacted`),
  `test_circuit_breaker.py` (trip/fast-fail/probe/recovery), `test_backup.py`
  (restorable SQLite, secret exclusion, retention, guarded restore),
  `test_tool_plugins.py` (sandbox-forced execute plugins, toggles),
  `test_streaming.py` (incremental `model.token`, identical accounting),
  `test_telemetry.py` (disabled-by-default, wiring), `test_a2a.py` (gated
  round-trip).

**Quality gates:** `make check` = `ruff check` (lint, line-length 100) + `ruff format --check`
+ `mypy --strict` + `pytest`. Type checking is **strict**; tests are written to
satisfy it. `web/smoke-test.mjs` is a headless dashboard smoke test. New code should
add a test in the matching directory and must keep the suite green before merge.

## 15. Bootstrapping & first run

`agentctl bootstrap` is the one-shot installer (`cli/main.py`): it runs the
environment probe, installs backend + (optionally) frontend deps, brings up Redis +
OpenConnector via Docker, initializes the SQLite schema (`alembic stamp head`), mints
the first admin token from `AGENT_BOOTSTRAP_SECRET`, and runs a smoke test against
`/health`.

**Quick start from a clean checkout:**

```bash
# Backend
cd backend && uv sync                # installs deps from pyproject + uv.lock
uv run alembic upgrade head          # create/upgrade the DB schema
make start                           # API server + RQ worker

# In another shell, try a goal (works offline in echo mode):
python3 ../cli/agentctl.py chat
#   → /help  then type a goal, or:
python3 ../cli/agentctl.py sessions create "explain the onboarding flow"
```

**Optional services** (`make up`):
- `docker compose up -d redis` — the RQ worker and scheduler (`redis_url`).
- `docker compose up -d openconnector` — self-hosted SaaS gateway on `:3000`; set
  `OPENCONNECTOR_BASE_URL=http://localhost:3000`.
- `ollama` (local) — set `DEFAULT_PROVIDER=ollama`; otherwise any of the 12 listed.

**First admin token:** `POST /api/v1/auth/token {"session_secret": "<AGENT_BOOTSTRAP_SECRET>"}`.
The CLI auto-mints from the local secret; the dashboard falls back to a signed
localhost cookie. Point your provider at `http://localhost:<api_port>/api/v1`.

## Appendix A. Environment variable catalog

Canonical names are the lower-cased attributes of `Settings` (`config.py`); the
`.env.example` lists all. Groups mirror §5.

**core** `agent_env`, `api_port` (8000), `api_session_secret`,
`agent_bootstrap_secret`, `redis_url`.

**storage** `database_url` (`sqlite:///data/agent_system.db`),
`vault_path` (`./data/vault`), `workspaces_dir`, `templates_dir`, `recordings_dir`,
`outputs_dir`, `skills_dir`, `soul_path` (`./SOUL.md`).

**providers** (key + optional `*_base_url`) — `ANTHROPIC_API_KEY`,
`GROQ_API_KEY`/`GROQ_BASE_URL`, `OLLAMA_BASE_URL`, `OPENAI_API_KEY`,
`OPENROUTER_API_KEY`/`OPENROUTER_BASE_URL`, `TOGETHER_API_KEY`, `MISTRAL_API_KEY`,
`GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `HUGGINGFACE_API_KEY`, `FREELLMAPI_API_KEY`,
`TOKENROUTER_API_KEY`, plus `DEFAULT_PROVIDER` (`echo`), `DEFAULT_MODEL`,
`PROVIDER_EXTRA_HEADERS` (JSON).

**tools** `tools_shell_mode` (`sandbox`), `tools_require_approval` (`false`)
(bool), `tools_max_iters` (8), `tools_fs_roots` (`[]`),
`tools_plugin_dir` (`tools_plugins`).

**memory** `memory_auto_remember` (`true`), `memory_recall_top_k` (3),
`memory_embedding_provider` (`hash` | `local`).

**limits** `max_context_tokens` (100000),
`context_compaction_threshold_pct` (75.0), plus v3.1 §30 limits (see cost/limits).

**routing** `circuit_breaker_threshold` (5),
`circuit_breaker_cooldown_seconds` (60).

**integrations** `OPENCONNECTOR_BASE_URL`, `OPENCONNECTOR_RUNTIME_TOKEN`,
`OPENCONNECTOR_ADMIN_TOKEN`, `OPENCONNECTOR_ALIAS` (`local`), `MCP_SERVERS` (JSON),
`OOMOL_CONNECT_ALLOWED_ACTIONS` (`*`), `A2A_ENABLED` (`false`).

**scheduler** `scheduler_enabled` (`true`), `scheduler_timezone` (`UTC`),
`backups_dir` (`backups`), `backup_retention_count` (7),
`backup_schedule_cron` (`0 3 * * *`).

**observability** `OTEL_EXPORTER_OTLP_ENDPOINT` (empty ⇒ telemetry off).

**telegram** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_IDS` (csv),
`TELEGRAM_WEBHOOK_SECRET` (empty ⇒ long-polling).

**cost/limits** `DAILY_BUDGET_USD` (10.0) + v3.1 §30 limits:
`MAX_CONCURRENT_AGENTS` (4), `MAX_CONCURRENT_TASKS` (8), `MAX_WORKSPACE_SIZE_MB`
(500), `MAX_FILE_SIZE_MB` (10), `MAX_OUTPUT_SIZE_MB` (50), `MAX_LOG_SIZE_MB` (10),
`MAX_BROWSER_SESSIONS` (3), `MAX_CONTAINER_CPU` (2.0), `MAX_CONTAINER_MEMORY_MB`
(2048), `MAX_EXECUTION_TIME_SECONDS` (1800), `MAX_TASK_TOKENS` (200000),
`MAX_TASK_COST_USD` (2.0), `MAX_RETRIES` (3).

## Appendix B. Canonical event types

Every event that crosses the bus is one `Event.type` string. The EventBus writes them
all to the `events` table (`infra/models.py`) with a monotonic `sequence`, so any
prefix in §9.6 / §4.2 can be tailed live or resumed after a reconnect.

```
# sessions
session.created   session.completed

# tasks (orchestrator: services/orchestrator.py)
task.planned      task.queued      task.started      task.completed
task.failed       task.cancelled   task.recovered

# agent run lifecycle
run.started       run.completed    run.failed        run.terminated

# recovery
recovery.started  recovery.completed  recovery.failed

# approvals (services/permissions.py via api/v1/router.py)
approval.requested  approval.decided  approval.expired   approval.granted

# model (services/model_router.py)
model.requested   model.completed    model.failed
model.token       model.circuit_opened  model.circuit_closed

# tools (services/agent_loop.py)
tool.called       tool.result        tool.error
context.compacted

# backups + a2a
backup.completed  backup.failed
a2a.delegated     a2a.result         a2a.failed

# cost (services/model_router.py:BudgetMonitor)
cost.recorded     cost.alert       (50/75/90/100% thresholds)

# workspaces + artifacts (api/v1/router.py)
workspace.created  workspace.modified  workspace.destroyed  artifact.created

# skills (api/v1/skills.py)
skill.created    skill.updated      skill.deleted

# recipes / batches
recipe.started   recipe.completed   recipe.failed
batch.created    batch.completed    batch.cancelled

# recordings + replay
recording.created  replay.started  replay.completed  replay.blocked

# insights + personality + scheduler + qa
insight.generated  personality.updated  personality.feedback  schedule.triggered
qa.report_created  qa.tests_completed
```

## Appendix C. Glossary

- **Agent System** — the FastAPI app (`agent_system/`); the whole backend runtime.
- **agentctl** — the Typer CLI in `cli/` (launcher `cli/agentctl.py`), proxies to the
  API. REPL command `chat`.
- **ReAct loop** — the provider-agnostic text loop in `services/agent_loop.py`
  (`run_tool_loop`) that parses fenced ` ```tool:name ` blocks and feeds
  `<tool_result>` back.
- **Tool fence** — ` ```tool:name {json} …``` `, the wire protocol between model and loop.
- **Skill** — a `SKILL.md` instruction pack (`services/skills.py`), injected into an
  agent prompt via `SkillManager.compose()`.
- **Soul** — `SOUL.md` identity, prepended as an `<identity>` block on every model call.
- **Provider** — an LLM backend. 12 implemented; `echo` is the offline deterministic one.
- **ModelRouter** — routes a call to one provider's adapter, composes prompt, records
  cost/events. The only prompt seam.
- **PermissionGate** — default-deny approval gate (`services/permissions.py`).
- **EventBus** — single canonical event pipeline (`infra/event_bus.py`); persists to
  SQLite + fans out over WS/SSE.
- **Supervisor** — goal → task DAG; **Orchestrator** — runs tasks + recovers crashes.
- **Recipe** — versioned task DAG; **Batch** — a parallel group of compatible tasks.
- **OpenConnector** — self-hosted SaaS-gateway image (`ghcr.io/oomol-lab/open-connector`);
  integrates as HTTP Runtime API and as an implicit MCP server.
- **MCP** — `McpHttpClient` (streamable HTTP, SSE+json) / `McpStdioClient` (subprocess),
  stdlib+httpx, no MCP SDK.
- **Sandbox** = Docker isolation for `execute`/shell tools (`services/sandbox.py`).
- **Workspace** = on-disk coding directory, path-guarded; `templates/` snapshot/clones them.
- **Vault** = Obsidian-style memory store (`VAULT_PATH`), recallable via `memory_*` tools.
- **Autopilot** = desktop-automation capability, off by default, approval-gated per
  action, kill-switch protected.
- **Insight** = event-derived summary (daily/weekly/anomaly), never fabricated.
- **Personality** = versioned per-agent tone/verbosity/reasoning (feedback-learnable,
  security-bounded).
- **Recording / Replay** = captured agent I/O (`recordings/*.jsonl`), replayable in
  INSPECT / SIMULATE / APPROVED_REEXEUTE modes with an environment fingerprint guard.
- **BudgetMonitor / cost** = per-scope spend limits (50/75/90/100% alerts) derived from
  real pricing; free-tier models at $0.00.
- **Agent types** — `llm` (ReAct via `react_agent`), `qa`, `browser`/`research`,
  `documents`; `_builtin` deterministic fallback. Registry: `agents/registry.py`.

---

*This document is the living implementation reference. When in doubt, the tests
(`backend/tests/`, **492 strong**) are the contract for what the code actually does.*























