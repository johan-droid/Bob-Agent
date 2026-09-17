# BOB Harness — Phase 0: Freeze + Baseline

> **Purpose:** freeze the *current, as-built* behaviour of the Bob Agent repository
> **before any harness modifications**, so every later phase can be diffed against a
> factual baseline (tests, architecture, tools, APIs, events, DB, frontend consumers).
>
> This document is evidence, not aspiration. Where the repo's own docs disagree with
> what the code/tests actually do today, the discrepancy is called out explicitly
> (see §9).

| Field | Value |
| --- | --- |
| Captured (UTC) | 2026-09-16T20:09:50Z |
| Branch | `main` |
| Commit | `cba76b29eb5bf7759078b8af00d9bbe5942f9ffe` |
| Working tree | **dirty** — 4 files modified, 1 untracked (see §2) |
| Repo root | `/home/ashutoshsahoo/Downloads/Bob Agent` |
| Backend root | `agent-system/backend` |
| Web root | `agent-system/web` |
| Python | 3.12.3 (`.venv`, uv-managed) |
| Node | v24.21.0 · npm 11.19.0 |
| pytest | 9.1.1 (`asyncio_mode = auto`) |
| ruff | 0.16.6 · mypy 2.3.1 (strict) |
| FastAPI | 0.141.1 · SQLAlchemy 2.0.52 · pydantic 2.13.5 |
| Next.js | 16.3.4 · React 19.1.0 · Electron 44.4.1 |

## 0. How this baseline was produced

| Step | Command / method |
| --- | --- |
| Run tests | `cd agent-system/backend && uv run pytest -q` (full suite) |
| Per-suite tests | `uv run pytest -q tests/{unit,contract,integration,security,recovery}` |
| Static gates | `uv run ruff check src tests` · `uv run ruff format --check src tests` · `uv run mypy src` |
| Architecture | read `docs/implementation/ARCHITECTURE.md` + source layout |
| Tool inventory | `build_registry(get_settings()).tools()` — **runtime introspection** (61 tools) |
| API inventory | `app.openapi()["paths"]` — **runtime introspection** (82 operations) |
| Event inventory | `domain/events.py` `EVENT_TYPES` + `event_extensions()` |
| DB state | `sqlite3 agent-system/backend/data/agent_system.db` (tables + row counts + event types) |
| Frontend consumers | `web/src/lib/api.ts` + `grep 'api\.' web/src/{app,components}` |

All counts below are machine-derived from the running code, not copied from docs.

## 1. Test baseline

**Full suite (`uv run pytest -q`), two independent runs — identical result:**

```
1 failed, 627 passed, 56 warnings in ~50s
FAILED tests/unit/test_telemetry.py::TestDisabledByDefault::test_unset_endpoint_is_zero_overhead_noop
```

| Suite | Collected | Result |
| --- | --- | --- |
| `tests/unit/` | 414 | ✅ 414 passed (in isolation) |
| `tests/contract/` | 72 | ✅ 72 passed |
| `tests/integration/` | 97 | ✅ 97 passed |
| `tests/security/` | 32 | ✅ 32 passed |
| `tests/recovery/` | 13 | ✅ 13 passed |
| **Total** | **628** | **627 passed, 1 failed (ordering-dependent)** |

### 1.1 The one failure — root-caused (pre-existing, not caused by this phase)

```
tests/unit/test_telemetry.py:51
    assert "opentelemetry" not in sys.modules
E   AssertionError
```

* The assertion assumes a pristine interpreter. It is a **test-ordering / isolation bug**,
  fully deterministic for the whole-suite run.
* Chain: pytest collects `tests/integration/test_worker.py` (line 20) →
  `agent_system.worker` (line 18: `from rq import Queue, Worker`) →
  **`rq` imports `opentelemetry` at import time** → `opentelemetry` lands in
  `sys.modules` before `tests/unit/` ever runs.
* Verified with an audit-hook probe: first polluted test nodeid is
  `tests/integration/test_worker.py::test_execute_task_succeeds_and_persists`.
* `tests/unit` alone → **414 passed**; `contract+integration+security+recovery` alone →
  **214 passed**; the failure appears *only* in the combined run.

**Freeze note:** this is a pre-existing red gate. Phase 0 records it, does not fix it.
Any harness work that changes test collection order will change this signal.

### 1.2 Extra runtime noise (not failures)

The telemetry smoke test (`tests/unit/test_telemetry.py::TestOtelSmoke`) sets
`OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` and exercises the real OTLP exporter.
With no collector listening, the agent prints repeated

```
Failed to export metrics batch due to timeout, max retries or shutdown.
```

plus a shutdown-time `ValueError: I/O operation on closed file` logging error. Cosmetic,
post-run, does not affect the exit code of any test.

## 2. Architecture (as built)

### 2.1 Layering (enforced by convention/review)

```
API            backend/src/agent_system/api/        FastAPI routers
Services       backend/src/agent_system/services/   application logic
Domain         backend/src/agent_system/domain/     events, lifecycles, ids, tasks
Infrastructure backend/src/agent_system/infra/      db, event bus, ORM models, telemetry
```

Dependency direction: API → services → domain → infra. Capabilities never issue raw SQL.

### 2.2 Execution spine

```
POST /api/v1/sessions/{id}/plan
  services/planner.py        Planner.plan(goal)          intent, DAG, capabilities, risk
  services/orchestrator.py   Supervisor.validate_plan    cycles, deps, limits, capabilities
                             Supervisor.apply_plan      persist tasks in topological order
                             Supervisor.plan            PENDING -> QUEUED for ready work

worker.py (RQ) | services/cloud.drive_session (in-process)
  services/orchestrator.py   Orchestrator.run_ready_tasks / _run_task
  agents/registry.py         resolve(agent_type) -> definition + handler
  agents/react_agent.py      llm_react_handler -> model calls + capability loop
  services/agent_loop.py     ReAct: parse ToolCall -> execute capability -> feed back
```

### 2.3 Capability pipeline (one execution seam)

```
model message
  services/tools/protocol.py   provider-native tool_calls  OR  ```tool:name fence
  services/tools/execution.py  validate args (schemas) -> require_capability -> run handler
  services/permissions.py      classify_risk -> PermissionGate.authorize = ALLOW|DENY|WAIT
```

Durable, DB-backed approvals (`approvals` table). `destructive` tier is default-deny and
not approvable. `DANGEROUS_SCOPES` + `destructive` cannot be granted by anyone.

### 2.4 Task state machine (`domain/tasks.py`)

States: `PENDING, PLANNING, QUEUED, RUNNING, BLOCKED_APPROVAL, RECOVERING, REVIEW,
SUCCEEDED, FAILED, CANCELLED`. Terminal: `SUCCEEDED, FAILED, CANCELLED`.
Invalid transitions raise `InvalidTransitionError` (never silently coerced).

### 2.5 Agent lifecycle (`domain/lifecycles.py`)

`CREATED → INITIALIZING → READY → RUNNING → {WAITING_TOOL, WAITING_APPROVAL, RECOVERING,
COMPLETED, FAILED, TERMINATED}`.

### 2.6 Agents declared (`agents/definitions.py` — 9)

`generic`, `code`, `research`, `browser`, `documents`, `qa`, `scheduler`, `llm`,
`builtin` — resolved by `agents/registry.py:resolve()`; an unregistered type routes to
`generic` and emits `agent.fallback_applied`.

### 2.7 Subsystems

* **Skills** — `skills/<name>/SKILL.md` (8 on disk: `agent-made`, `cool-skill`,
  `demo-skill`, `document-craft`, `for-all`, `for-research`, `qa-assist`,
  `web-research`). Procedural only; cannot execute or bypass permissions.
* **Memory/Vault** — Obsidian markdown vault (`VAULT_PATH`) with layered notes.
* **Sandbox** — `services/tools/builtin/_exec.py:run_workspace_command` →
  `DockerSandbox | SubprocessJail | local | off`. QA image `agent-system/qa-sandbox:latest`
  is a local build artifact; a missing image fails closed.
* **MCP / OpenConnector** — optional tools, registered only when configured.
* **Telemetry** — no-op unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set.

### 2.8 In-flight (uncommitted) changes captured in this freeze

The working tree is **dirty**; the baseline above was measured *with* these changes.

```
modified: config.py       +15   (planner_use_llm, planner_model,
                                 verifier_enabled, verifier_use_llm_judge, verifier_strict)
modified: planner.py      +247  (LLM planner path, strategy "llm_model_v1", cycle check)
modified: orchestrator.py +152  (REVIEW gate: RUNNING -> REVIEW -> SUCCEEDED/FAILED)
modified: worker.py       +104  (mirrors the REVIEW gate for the RQ worker path)
untracked: services/verifier.py +292  (post-execution Verifier: deterministic checks,
                                 sandboxed QA, optional LLM judge, lenient by default)
```

Behavioural effect already live in tests: a successful handler result now passes through
`REVIEW` and emits `qa.started` → `qa.completed|qa.failed`, and the task result gains a
`verification` envelope. Defaults are lenient (`verifier_enabled=True`,
`verifier_strict=False`), so unevidenced results still pass with a recorded reason.

## 3. Tool inventory — 61 capabilities (runtime)

`build_registry(get_settings())`, deterministic registration order
(`filesystem, coding, git, shell, browser, research, documents, memory, tasks, system`).
Optional groups (`openconnector`, `mcp`) + plugins merge last and are **absent** in this
offline config, so the live total is 61.

Risk distribution: **read 32 · write 13 · execute 13 · destructive 3.**

| Group | Count | Capabilities (risk tier) |
| --- | --- | --- |
| filesystem | 9 | file_read, file_list, file_search, file_metadata, file_diff, directory_tree (read) · file_write, file_edit, file_patch (write) |
| coding | 11 | read_source, repo_search, project_detect, inspect_build, inspect_dependencies (read) · edit_source, apply_patch (write) · run_tests, run_linter, run_typecheck, run_formatter (execute) |
| git | 12 | git_status, git_diff, git_log, git_show, git_branch_list (read) · git_add, git_commit, git_checkout, git_restore (write) · git_clean, git_reset_hard, git_push_force (**destructive**) |
| shell | 1 | shell (execute, dynamic scope) |
| browser | 9 | browser_session (read) · browser_open, browser_navigate, browser_click, browser_type, browser_select, browser_scroll, browser_extract, browser_screenshot (execute) |
| research | 7 | research_search, research_fetch, research_extract, research_citations, source_metadata, compare_sources, web_fetch (read) |
| documents | 6 | document_extract, document_inspect, document_validate (read) · document_create, document_convert, document_persist (write) |
| memory | 2 | memory_recall (read) · memory_remember (write) |
| tasks | 2 | task_status, tasks_inspect (read) |
| system | 2 | capabilities_list, system_status (read) |
| *(optional, not registered offline)* | — | openconnector_execute, openconnector_list (group `openconnector`); mcp_call, mcp_list (group `mcp`) |

Model providers (`services/providers.py`): **12** — openai, anthropic, groq, ollama
(keyless), openrouter, together, mistral, gemini, deepseek, huggingface, freellmapi,
tokenrouter — plus offline **echo** mode.

## 4. API inventory — 82 documented operations (+1 hidden)

Derived from `app.openapi()["paths"]` (FastAPI lazily includes routers, so scraping
`app.routes` under-reports). All paths are under `/api/v1`. Auth = bearer token unless noted.

| Domain | Ops | Endpoints |
| --- | --- | --- |
| health/ready | 2 | `GET /health`, `GET /ready` *(unauth)* |
| auth | 1 | `POST /auth/token` *(unauth, bootstrap secret, 10 req/min per IP)* |
| sessions | 6 | `POST /sessions`, `GET /sessions`, `GET /sessions/{id}`, `PATCH /sessions/{id}`, `DELETE /sessions/{id}`, `POST /sessions/{id}/plan` |
| tasks | 6 | `POST /tasks`, `GET /tasks`, `GET /tasks/{id}`, `POST /tasks/{id}/transition`, `POST /tasks/{id}/retry`, `POST /tasks/{id}/run` |
| approvals | 4 | `POST /approvals`, `GET /approvals`, `POST /approvals/{id}/decision`, `POST /approvals/sweep` |
| workspaces | 8 | `POST/GET /workspaces`, `GET/DELETE /workspaces/{id}`, `GET /workspaces/{id}/tree`, `GET/PUT /workspaces/{id}/file`, `GET /workspaces/{id}/fingerprint`, `POST /workspaces/{id}/exec` |
| artifacts | 2 | `GET /artifacts`, `GET /artifacts/{id}` |
| events | 3 | `GET /events`, `GET /events/stream` (SSE), `GET /events/latest-sequence` |
| vault | 4 | `GET /vault/notes`, `GET /vault/note`, `POST /vault/notes`, `DELETE /vault/notes` |
| templates | 4 | `GET /templates`, `POST /templates`, `POST /templates/{id}/restore`, `DELETE /templates/{id}` |
| recordings | 2 | `GET /recordings`, `POST /recordings/{id}/replay` |
| batches | 3 | `POST /batches`, `GET /batches/{id}`, `POST /batches/{id}/cancel` |
| recipes | 5 | `GET/POST /recipes`, `GET /recipes/{id}`, `POST /recipes/{id}/execute`, `POST /recipes/{id}/cancel` |
| personality | 4 | `GET/PUT /personality/{agent_id}`, `POST /personality/{agent_id}/feedback`, `POST /personality/{agent_id}/learn` |
| insights | 3 | `GET /insights`, `POST /insights/generate`, `POST /insights/{id}/archive` |
| schedule | 3 | `GET /schedule`, `POST /schedule`, `DELETE /schedule/{job_id}` |
| autopilot | 3 | `GET /autopilot/status`, `POST /autopilot/kill`, `POST /autopilot/reset` |
| model-calls | 1 | `GET /model-calls` |
| model-routing | 2 | `GET /model-routing/providers`, `POST /model-routing/test` |
| qa-reports | 1 | `GET /qa-reports` |
| settings | 3 | `GET /settings`, `GET /settings/{key}`, `POST /settings/{key}` |
| settings-groups | 1 | `GET /settings-groups` |
| skills | 6 | `GET/POST /skills`, `GET/PATCH/DELETE /skills/{name}`, `POST /skills/import` |
| telegram | 2 | `GET /telegram/status`, `POST /telegram/webhook` *(header-auth)* |
| a2a | 3 | `POST /a2a/delegate`, `POST /a2a/callback` *(HMAC, no bearer)*, `GET /a2a/delegations` |
| **hidden** | 1 | `GET /ready-dependency-check` (`include_in_schema=False`, auth) |

Frontend proxy: `web/src/app/api/v1/[...path]/route.ts` forwards GET/POST/PUT/PATCH/DELETE
to `AGENT_SYSTEM_API_URL` (default `http://127.0.0.1:8000`), auto-minting a bearer token from
`AGENT_BOOTSTRAP_SECRET` (cached 1h, refresh-on-401, 503 if unavailable, 502 if unreachable).

## 5. Event inventory — 52 canonical + 8 registered extensions (60 total)

`validate_event_type()` rejects anything outside the taxonomy; unknown names raise
`UnknownEventTypeError` (never silently persisted). The bus (`infra/event_bus.py`)
redacts secret-bearing payload keys, assigns a globally monotonic `sequence`, persists to
`events`, and fans out to subscribers. It is the only bus.

| Domain | Canonical events |
| --- | --- |
| session (4) | session.created, updated, completed, deleted |
| task (8) | task.created, queued, started, completed, failed, cancelled, recovering, blocked_approval |
| agent (8) | agent.created, started, waiting_tool, waiting_approval, completed, failed, terminated, fallback_applied |
| model (6) | model.requested, completed, failed, token, circuit_opened, circuit_closed |
| tool (3) | tool.started, completed, failed |
| approval (4) | approval.requested, approved, denied, expired |
| workspace (4) | workspace.created, modified, destroyed, restored |
| artifact (2) | artifact.created, deleted |
| context (1) | context.compacted |
| qa (3) | qa.started, completed, failed |
| recovery (3) | recovery.started, completed, failed |
| recipe (3) | recipe.started, completed, failed |
| cost (2) | cost.recorded, cost.alert |
| insight (1) | insight.generated |

Registered extensions (`register_event_extensions(owner, types)`):

| Owner | Events |
| --- | --- |
| skills | skill.created, skill.updated, skill.deleted |
| a2a | a2a.delegated, a2a.result, a2a.failed |
| backup | backup.completed, backup.failed |

Envelope (`Event`): `event_id, schema_version, session_id, task_id, agent_run_id, sequence,
timestamp, type, actor, payload, visibility (user|internal), sensitivity (normal|sensitive)`.

## 6. Database state

Authoritative store: SQLite (WAL, `foreign_keys=ON`, `busy_timeout=5000`,
`synchronous=NORMAL`). Redis (when present) carries only reconstructible queue state.

* File: `agent-system/backend/data/agent_system.db` (~1.2 MB + `-wal`/`-shm`)
* Alembic head: **`e6f7a8b9c0d1`** (permission-gate persistence)
* Tables: **23** (incl. `alembic_version`)

| Table | Rows | Table | Rows |
| --- | --- | --- | --- |
| events | 1743 | agent_runs | 66 |
| tasks | 266 | approvals | 51 |
| sessions | 231 | workspaces | 42 |
| feedback_log | 210 | agent_personalities | 22 |
| behavior_recordings | 21 | insights | 21 |
| recipes | 21 | task_batches | 21 |
| model_calls | 8 | agent_leases | 3 |
| alembic_version | 1 | artifacts | 0 |
| cost_budget | 0 | idempotency_keys | 0 |
| memory_notes | 0 | qa_reports | 0 |
| scheduled_jobs | 0 | tool_calls | 0 |
| workspace_templates | 0 | *(23 tables total)* | — |

**Observed distributions**

* `tasks.state`: PENDING 148 · CANCELLED 43 · QUEUED 34 · SUCCEEDED 26 · FAILED 12 · RUNNING 3
* `sessions.status`: ACTIVE 231
* `approvals.decision`: DENIED 20 · APPROVED 14 · EXPIRED 13 · PENDING 4
* `model_calls.provider`: openrouter 8

**Observed event types in `events`** (29 distinct, top by volume):
`session.created 502 · task.created 266 · skill.created 144 · task.queued 138 ·
task.started 127 · agent.started 66 · approval.requested 61 · recovery.started 47 ·
recovery.completed 45 · skill.updated 36 · task.failed 31 · model.requested 28 ·
task.completed 26 · insight.generated 21 · recipe.started 21 · workspace.created 21 ·
workspace.modified 21 · workspace.restored 21 · session.deleted 19 · session.updated 19 ·
approval.approved 18 · skill.deleted 18 · approval.expired 13 · cost.recorded 8 ·
model.failed 8 · agent.completed 6 · agent.failed 6 · approval.denied 3 ·
recovery.failed 2 · task.cancelled 1`.

Note: this is a **developer/dev-data** database (231 sessions, 266 tasks). The test suite
never touches it — `tests/conftest.py` migrates a disposable template and every test gets a
per-test DB via `DATABASE_URL` monkeypatch.

## 7. Frontend consumers (Next.js dashboard)

15 `page.tsx` routes: `/` (dashboard), `chat`, `kanban`, `workspace`, `vault`, `outputs`,
`schedule`, `approvals`, `templates`, `cost`, `recipes`, `reasoning`, `insights`, `audit`,
`settings`. Plus `components/` (Sidebar, Header, ChatSidebar, CommandPalette, MessageBubble,
Toast, ThemeToggle, ErrorBanner, EmptyState, Skeleton, LoadingSpinner) and
`api/v1/[...path]/route.ts` (the auth-injecting proxy).

The typed client `web/src/lib/api.ts` (`API_BASE = "/api/v1"`, real backend only) exposes:

| Page / component | Client methods consumed |
| --- | --- |
| `page.tsx` | `health`, `ready`, `listSessions`, `listTasks` |
| `chat` | `listSessions`, `createSession`, `updateSession`, `deleteSession`, `listTasks`, `createTask`, `runTask`, `cancelTask`, `listApprovals`, `decideApproval`, `listRoutingProviders` |
| `kanban` | `listTasks`, `eventsAfter(0,1000)`, `retryTask`, `transitionTask` |
| `approvals` | `listApprovals`, `decideApproval`, `sweepApprovals` |
| `workspace` | `listWorkspaces`, `createWorkspace`, `deleteWorkspace`, `readWorkspaceFile`, `writeWorkspaceFile` |
| `templates` | `listTemplates`, `createTemplate`, `restoreTemplate`, `deleteTemplate`, `listWorkspaces` |
| `vault` | `listVaultNotes`, `createVaultNote`, `deleteVaultNote` |
| `outputs` | `listArtifacts`, `getArtifact` |
| `reasoning` | `listRecordings`, `replay(id, "INSPECT")` |
| `recipes` | `listRecipes`, `getRecipe`, `executeRecipe`, `cancelRecipe` |
| `insights` | `listInsights`, `generateInsight`, `archiveInsight` |
| `schedule` | `listScheduledJobs`, `createScheduledJob`, `deleteScheduledJob` |
| `cost` | `listModelCalls` |
| `settings` | `listSettings`, `setSetting`, `listRoutingProviders`, `testModelProvider` |
| `Sidebar` | `health`, `listApprovals(true)`, `listTasks` |
| `Header`, `placeholder` | `health` |
| `CommandPalette` | `listSessions` |

Client-side live updates use `EventPoller` (resume-from-sequence polling every 1500 ms via
`eventsAfter`), not WebSocket. Backend also serves SSE at `/events/stream`.

**Frontend static baseline:** `npx tsc --noEmit` → **exit 0** (clean).
`npm run build` and `node smoke-test.mjs` were **not** run in this phase (smoke test needs the
built app + headless Chromium/Playwright).

## 8. Entry points & smoke commands

| Surface | Command |
| --- | --- |
| API | `cd agent-system/backend && uv run uvicorn agent_system.api.main:app --port 8000` |
| Worker (RQ) | `uv run python -m agent_system.worker` |
| CLI | `agentctl` (install `uv pip install -e cli/`); `agentctl doctor`, `agentctl chat`, `agentctl setup`, `agentctl settings …`, sub-apps: `workspace tasks approvals sessions events backup tools skills soul settings` |
| Migrations | `uv run alembic upgrade head` (head `e6f7a8b9c0d1`) |
| Full test run | `cd agent-system/backend && uv run pytest -q` |
| Lint / format / types | `uv run ruff check src tests` · `uv run ruff format --check src tests` · `uv run mypy src` |
| Web | `cd agent-system/web && npm run dev` (dev) · `npm run build` · `npm start` · `npm run app` (Electron desktop) |
| QA sandbox image | `make qa-sandbox-image` (builds `agent-system/qa-sandbox:latest` locally) |
| Concurrency | `make start` (API + RQ worker), `make up` (Redis + OpenConnector via docker compose) |

CI (`.github/workflows/ci.yml`) gates: `ruff`+`mypy`, full `pytest`, `pytest tests/security`,
`alembic upgrade head` + `alembic check` + idempotent upgrade, then web `tsc --noEmit` +
`npm run build` + `node smoke-test.mjs` (with headless Chromium).

## 9. Static-gate status & discrepancies (important for the freeze)

The repo's own docs (`docs/implementation/REPOSITORY_INVENTORY.md`) claim "ruff clean ·
ruff format clean · mypy --strict clean". Measured **today with the installed toolchain**,
that is **not** true. These are pre-existing (the failures include committed files), and are
recorded here so later phases don't mistake them for regressions.

### 9.1 `ruff check src tests` → **3 errors (E501 line > 100)**

| File | Line |
| --- | --- |
| `services/orchestrator.py` | 598 |
| `services/planner.py` | 384 |
| `services/verifier.py` | 230 |

(All three are in the uncommitted/in-flight verifier work.)

### 9.2 `ruff format --check src tests` → **7 files would be reformatted**

`api/v1/router.py`, `cli/chat.py`, `services/orchestrator.py`, `services/planner.py`,
`services/verifier.py`, `tests/integration/test_permission_path.py`,
`tests/unit/test_memory_embeddings.py`. (151 already formatted.)

### 9.3 `mypy src` → **12 errors in 2 files (checked 98 source files)**

`11 × unused-ignore` in `infra/telemetry.py` (lines 191,193,196,199,200,203,204,205,254,262,270)
plus `1 × unused-ignore` in `services/memory.py:238`. Cause is the installed **mypy 2.3.1**,
which flags `# type: ignore` comments that are no longer needed — i.e. a toolchain-version
drift versus the documented "clean" state, not a logic defect.

### 9.4 Documentation vs measurement

| Claim in docs | Measured baseline |
| --- | --- |
| "605 passed, 0 failed, 9 skipped" | **628 collected: 627 passed, 1 failed** (ordering bug §1.1); no skips observed |
| "23 tables" | ✅ 23 tables confirmed |
| "head e6f7a8b9c0d1" | ✅ confirmed |
| "17 web routes" | 15 `page.tsx` (+ `layout`, `placeholder`, dynamic proxy) |
| "Currently deterministic planner (`deterministic_keyword_v1`)" | In-flight `planner.py` adds `llm_model_v1`; `planner_use_llm=True` default |
| "No verifier / REVIEW orphaned" | In-flight `verifier.py` + REVIEW gate now active (lenient) |
| "ruff/mypy clean" | ❌ see §9.1–9.3 |

### 9.5 Known gaps (from repo docs, still open)

Browser live-capture needs Playwright; APScheduler service enable/trigger endpoints;
sub-agent spawn + todos + vault API wiring; some UI wiring (auth/skills/providers/soul/schedule);
real OpenConnector named connections need operator keys. `artifacts`, `tool_calls`,
`qa_reports`, `memory_notes`, `cost_budget`, `idempotency_keys`, `scheduled_jobs`,
`workspace_templates` are empty in the dev DB.

## 10. Freeze rules for Phase 0

1. **Do not "fix" anything while freezing.** The single failing test (§1.1) and the static
   gates (§9) are *inputs* to later phases, not Phase 0 chores.
2. **Baseline includes the dirty working tree.** Any Phase 1+ change must be diffed against
   the state described in §2.8, not against `HEAD`.
3. **Re-measure, don't trust docs.** Counts in §3–§7 come from runtime introspection; the
   project docs are stale in several places (§9.4).
4. **The harness must not mutate production paths.** `agent-system/backend/data/agent_system.db`,
   `vault/`, `workspaces/`, `recordings/`, `outputs/` are live dev data. Tests already isolate
   themselves (§6); harness tooling must do the same.
5. **One execution seam, one bus, one gate.** Any harness instrumentation should attach to
   `services/tools/execution.py`, `infra/event_bus.py`, and `services/permissions.py` rather
   than adding parallel paths.

### Reproduction (copy/paste)

```bash
# tests
cd "agent-system/backend"
uv run pytest -q                                   # -> 1 failed, 627 passed (see §1.1)
uv run pytest -q tests/unit                        # -> 414 passed
uv run pytest -q tests/contract tests/integration tests/security tests/recovery  # -> 214 passed

# static gates
uv run ruff check src tests                        # -> 3 errors
uv run ruff format --check src tests               # -> 7 unformatted
uv run mypy src                                    # -> 12 errors

# inventories
uv run python - <<'PY'
from agent_system.api.main import app
from agent_system.config import get_settings
from agent_system.domain.events import EVENT_TYPES, event_extensions
from agent_system.services.tools.registry import build_registry
print("routes", len(app.openapi()["paths"]))
print("tools", len(build_registry(get_settings()).tools()))
print("events", len(EVENT_TYPES), "+", len(event_extensions()), "extensions")
PY

# web
cd ../web && npx tsc --noEmit                      # -> exit 0
```

## 11. Phase 0 acceptance checklist

| Requirement | Status | Evidence |
| --- | --- | --- |
| Run tests | ✅ | §1 (628 collected; 1 pre-existing failure, root-caused) |
| Inspect architecture | ✅ | §2 |
| Inventory tools | ✅ | §3 (61 runtime tools, risk + group) |
| Inventory APIs | ✅ | §4 (82 ops + 1 hidden) |
| Inventory events | ✅ | §5 (52 canonical + 8 extensions) + observed-in-DB list |
| Inventory DB state | ✅ | §6 (23 tables, row counts, distributions, head) |
| Inventory frontend consumers | ✅ | §7 (15 pages, client methods, proxy, tsc) |
| Create `BOB_HARNESS_BASELINE.md` | ✅ | this file |

---

*Frozen at commit `cba76b2` + working-tree changes, 2026-09-16T20:09:50Z.*






