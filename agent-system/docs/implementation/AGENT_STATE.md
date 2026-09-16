# AGENT_STATE

> Handoff document for any autonomous agent resuming this build (v3.1 §37).
> **Synced 2026-09-07** — all 21 phases implemented; runtime verification gated only by Docker/Redis availability. See `STATUS.md` for the full phase table.

## Current Phase

**Build complete (Phases 0–21).** Recent milestones:
- **Phase 20 — Real LLM task execution** (`react_agent.py`): goal → ModelRouter + `run_tool_loop` with the full tool registry (shell/files/web/memory/MCP/OpenConnector); worker + orchestrator wiring; goal-aware fallback (echo builtin offline — honest). Fixed per-instance EventBus sequence collision (bus now shared via execution context) and echo router self-heal.
- **Phase 21 — OpenConnector + MCP over HTTP**: real Runtime API client (`/v1/actions`, guides, health) + streamable-HTTP MCP transport (`POST /mcp`, SSE/JSON, `Mcp-Session-Id`) + implicit `openconnector` MCP server + discovery tools; **live-verified against `ghcr.io/oomol-lab/open-connector:latest`**.

Remaining work:

1. ~~Redis/RQ worker verification~~ **DONE**: redis:7-alpine up via compose; worker executed a task out-of-process (`worker_id=agent-worker-pop-os-…` in AgentRun); kill -9 mid-task → lease reaper recovered (RECOVERING→QUEUED), no duplicate runs. Two fixes landed: unique worker names (stale registration no longer blocks restart) and attempt semantics (counts "times started", incremented only at RUNNING — reaper/retry no longer double-increment).
2. Docker-gated tests (sandbox escape, browser-live, QA-in-Docker) — Docker daemon present; deps for the sandbox image may still be needed.
3. Vault + Templates tab APIs (`/api/v1/vault/notes`, `/api/v1/templates`) → convert the last two honest placeholder tabs.
4. APScheduler runtime wiring: `/schedule` APIs already store jobs; the APScheduler service + enable/trigger endpoints are the next backlog item.
5. E2E acceptance (master plan §7) after clean install + full restart.

## Completed Phases (build state)

- **Phase 0:** repo scaffold, uv tooling, ruff/mypy/pytest, Makefile, docker-compose, .env.example, docs baseline.
- **Phase 1:** canonical prefixed-ULID IDs, event envelope + catalog, 10-state task machine, SQLite WAL/FK/busy-timeout, 22-table schema, Alembic `94be8eadb99f`, restart tests.
- **Phase 2:** EventBus — monotonic sequence, emit-time redaction, replay, **idempotent event_id dedupe** (duplicate delivery absorbed, no sequence burned) + WS/SSE fanout (`api/v1/realtime.py`) with `after_sequence` resume.
- **Phase 3:** Authenticator + `/api/v1` routers (sessions, tasks, transitions, retry, approvals, events, workspaces, exec, artifacts) + PermissionGate (4 risks × 5 policies, default-deny scopes, expiry sweeper) + contract tests.
- **Phase 4:** Supervisor DAG + in-process Orchestrator (leases, reaper, cancellation, orphan recovery) **and** RQ worker (`worker.py`, `python -m agent_system.worker`). **Live-verified 2026-09-06:** happy path + kill -9 crash recovery both pass E2E.
- **Phase 5:** WorkspaceManager (traversal-safe, 10 MB cap, fingerprint) + DockerSandbox (CPU/mem limits, network off by default) + TemplateManager (pre-snapshot secret scan).
- **Phase 6:** BrowserAgent (isolated Playwright contexts, recording, `browser:*` scopes) + ResearchAgent (citations); optional imports degrade gracefully.
- **Phase 7:** DocumentAgent — deterministic PPTX/DOCX/XLSX/PDF + `/api/v1/artifacts`.
- **Phase 8:** Obsidian writer (frontmatter, wiki-links, attribution, secret scrub) + MemoryStore (layers, EmbeddingProvider, hashed-lexical default).
- **Phase 9:** Next.js 16.3 dashboard — 17 routes build ✓. **Wired to live APIs:** Chat, Kanban, Approvals, Workspace, Outputs, Audit, Recipes, Insights, Cost, Reasoning(Trace), Schedule, Settings. **Honest placeholders:** Vault, Templates. EventPoller implements resume-from-sequence.
- **Phase 10:** `agentctl` CLI — thin `/api/v1` client, `--json`, documented exit codes, stub-backend contract tests.
- **Phase 11:** ModelRouter + PricingRegistry + BudgetMonitor + ModelCall rows + `/model-calls` API + Cost tab.
- **Phase 12:** Recovery pipeline (Introspector→Planner→Executor→PatternLearner); never auto-retries destructive/permission/validation/identical-repeat.
- **Phase 13:** QA agent — generated tests as untrusted code (Docker sandbox preferred, isolated-subprocess fallback, never in-process).
- **Phase 14:** templates — tar snapshots + secret-exclusion scan + version metadata + clone.
- **Phase 15:** **recording + replay** — `BehaviorRecorder` (append-only `.jsonl`, redact_dict scrubbing, MAX_STEPS cap, header carries ReplayContext fingerprint), `RecordingContext` (wraps agent I/O), `ReplayService` with **INSPECT** (always allowed, reports fingerprint diff), **SIMULATE** (dry-run handlers, zero side effects), **APPROVED_REEXECUTE** (requires fingerprint match AND fresh approval; mismatch blocks). API: `GET /recordings`, `POST /recordings/{id}/replay` (403 on blocked). Tests: 15.
- **Phase 16:** **batching + recipes** — `TaskBatcher` (compatibility check: same agent/task type, no inter-deps, PENDING/QUEUED only; partial-failure isolation; queued-only cancel; speedup metric), `RecipeEngine` (versioned validated DAG JSON — shape, dupes, unknown deps, cycles; `{{param}}` substitution; executes through the canonical Supervisor pipeline; cancel). API: `/batches`, `/recipes`. Tests: 19.
- **Phase 17:** **personality + insights** — `PersonalityManager` (versioned configs; update rejects non-adjustable fields — security/permission/safety structurally unreachable; feedback loop infers prompt-only adjustments at N=10 ratings), `InsightGenerator` (reads ONLY canonical events; empty history → honest empty insight; anomaly detection with suggested actions; scrub_text before persistence), `/schedule` persisted jobs API. APIs: `/personality/{id}` (+feedback/learn), `/insights` (+generate/archive). Tests: 8.
- **Phase 18:** **autopilot** — `AutopilotService`: **off by default** (`enabled=False` in app factory), refuses every run when disabled; per-action approval binding (fresh, unexpired, APPROVED, `requested_action == autopilot:<action>` else denied); hard default-deny (payments/credentials/account actions + unknown actions, cannot be approved away); caps (50 actions/run, 300 s); **sticky kill switch** (`kill()` idempotent, blocks enable until explicit `reset()`); complete audit trail (refused/denied/forbidden/executed/failed). Executor injected at composition — no pyautogui/pynput import in the module. API: `/autopilot/status|kill|reset`. Tests: 17 security tests.
- **Phase 19:** **hardening** — chaos/restart suite (`tests/recovery/test_phase19_hardening.py`): duplicate-event dedupe, replay stability, fresh-engine restart persistence, crash-reopen DB, approval expiry fails closed, replay blocked after workspace mutation, batch partial failure, worker double-delivery idempotency. FINAL_REPORT.md written.
- **Phase 20:** **real LLM task execution** — `agents/react_agent.py` `llm_react_handler`: goal → `ModelRouter.invoke` wrapped in `run_tool_loop`; tool events `tool.called/result`; memory recall → prompt, outcome → vault; `install()` registers the handler + goal-aware default for unknown agent types. Worker + orchestrator pass `factory`/`bus`/`agent_type` through the execution context (shared EventBus fixes per-instance sequence collisions). `_build_router` self-heals offline defaults (echo pricing + adapter). Tests: 7.
- **Phase 21:** **OpenConnector + MCP over HTTP** — `services/openconnector.py` against the real `oomol-lab/open-connector` Runtime API (`POST /v1/actions/:id` `{input, connectionName}`, `GET /v1/actions[?service=]`, `GET /health`, best-effort catalog, action guides via admin API; `success`/`data` envelope + `ConnectorError(code,status)`); `services/mcp.py` gains `McpHttpClient` (streamable HTTP: JSON + SSE `event: message`/`data:`, `Mcp-Session-Id` replay, 202 notification tolerance) + config `url`/`headers`/`alias`; implicit `openconnector` MCP server auto-appended in `all_servers()`; tools `openconnector_execute`/`openconnector_list`/`mcp_list`; `/tools` CLI introspection. Docker-compose adds the optional gateway (port 3000). **Live-verified** against the real container (action discovery/execution/guide + MCP `tools/call`). Tests: 12.

## Current Task

**Done (2026-09-07).** Latest verified state:
- Suite **427 passed** (1 env-gated Docker failure); ruff clean; mypy --strict clean (62 files).
- ReAct + tools + real LLM execution wired in worker/orchestrator (Phase 20).
- OpenConnector HTTP + MCP integration live-verified (Phase 21).
- Setup wizard compacted; chat REPL now a full command system: `/model set|test`, `/settings`, `/tools`/`/mcp`, `/memory`, `/recall`, `/schedule`, plus the pre-existing session/approval commands.

None in flight — clean stopping point.

## Active Blockers (environment, not code)

- **Docker daemon present**, but 1 test still fails (`test_real_docker_sandbox_runs_untrusted_test`) — needs inspection of the sandbox image/deps.
- **Redis: live-verified** — no longer a blocker.

## Tests Last Executed

`uv run pytest -q` → **427 passed, 1 env-gated failure** (2026-09-07).
ruff: All checks passed. mypy --strict: Success (**62 files**). `npm run build` (web): ✓ established earlier.
E2E worker scripts: `scripts/e2e_worker_check.py` (happy path ✓) and `scripts/e2e_crash_recovery_check.py` (kill -9 recovery ✓).
OpenConnector live check (2026-09-07): real container — `/health` ✓, action discovery ✓, `hackernews.get_top_stories` execution ✓, action guide ✓, HTTP MCP `initialize → tools/list → tools/call` with session-id replay ✓.

## Known Failures

- `test_real_docker_sandbox_runs_untrusted_test` — requires Docker daemon. Environment, not code.

## Architecture Decisions (see also vault note 22_Decision_Log)

- EventBus dedupe: `emit` checks `session.get(EventRow, event_id)` first — duplicates return the persisted row untouched (idempotent, no sequence burn, no double fanout).
- EventBus sequence is global (not per-session); per-session ordering follows from global monotonic order.
- **EventBus instances are shared via the execution context** — sequence counters are per-instance, so a second bus in the same process collides on insert. The worker/orchestrator pass their bus into `run_agent` context; `react_agent` reuses it.
- `session.flush()` inside `EventBus.emit` so events are queryable in-transaction.
- Task `FAILED` allows explicit operator-driven retry, never automatic.
- Playwright + document libraries are optional imports with clear errors.
- QA untrusted tests: Docker sandbox preferred; documented weaker isolated-subprocess fallback; never in-process.
- **ReAct protocol is text-fenced, not provider-native function calling** — fenced ```` ```tool:name ```` blocks + `<tool_result>` feedback work identically on every provider (OpenAI/Anthropic/Gemini/Groq/Ollama/echo). No provider SDK glue required.
- **Honest fallback:** unknown agent types route to the LLM loop only when a real provider is configured *and* the task input carries a goal; otherwise the deterministic builtin (`registry._builtin`) runs. No fake "LLM did it" results offline.
- **OpenConnector envelope:** the runtime uses `{"success": true/false, "data": …, "error": …, "errorCode": …}` — `_unwrap` maps non-2xx *and* `success:false` into `ConnectorError(code, status)`.
- **MCP HTTP transport must own its `Accept` header** (`application/json, text/event-stream`) — the implicit OpenConnector server forwards only auth/alias headers; a leaked `Accept: application/json` causes 406 `Not Acceptable`.
- **Attempt semantics (fixed via live E2E):** `tasks.attempt` counts "times execution started" — incremented at every entry into RUNNING (worker `execute_task`, orchestrator `_run_task`, manual API transition), NEVER on requeue (reaper, retry endpoint). Unit tests previously passed because each path was exercised in isolation; the end-to-end run exposed the double-count.
- **Worker naming (fixed):** RQ workers register with a unique per-process name (`agent-worker-<host>-<uuid8>`); a crashed worker's stale Redis registration previously blocked restart with "active worker named 'agent-worker' already".
- Autopilot approval model: the service never self-approves — operators approve via the API, then the run binds each action to that exact approval record; DENY-by-default even for approved-scope reuse across different actions.
- Personality feedback loop touches only `tone/verbosity/reasoning_style/system_prompt_override`; `update()` rejects any other key (security bounding by construction).
- Insights read only the `events` table — no private tables, no fabricated numbers when history is empty.
- Replay fingerprints cover workspace content, OS, deps, agent version, model config, recipe version, permissions.
- CLI has zero business logic — one `api_request` gateway maps errors to exit codes 0/2/3/4/5/6.

## Remaining Work

1. Runtime verification with Redis + Docker (see Current Phase).
2. Vault/Templates list APIs → last two placeholder tabs.
3. APScheduler service + enable/trigger endpoints (schedule CRUD APIs exist; the loop itself is next).
4. Docker sandbox image deps → unblock the one env-gated test.
5. E2E acceptance (master plan §7) + FINAL_REPORT label flips.

## Known Limitations

- Autopilot runs in-process via the injected executor; the restricted-OS-account execution mode is a deployment concern (run the executor as the sandbox user).
- Recipe cancellation targets the latest run's session (runs are tracked by session prefix; a `recipe_runs` table would make history first-class).
- `web/` Vault/Tabs placeholders state exactly what is implemented vs pending (§33-compliant).
- API auth uses bootstrap-secret minting; rotate `AGENT_BOOTSTRAP_SECRET` before any non-localhost use.
- OpenConnector `/v1/catalog` is a hosted-only endpoint on self-hosted runtimes — `catalog_summary` falls back to counting `list_actions`.
- MCP SSE "ping/keep-alive" frames are ignored (fine for a request/response client); long-lived streaming tool results (e.g. `resources/stream`) are out of scope.
