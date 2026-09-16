# STATUS

> **Updated:** 2026-09-07 · **Current phase:** 0–21 all phases implemented; real LLM task execution (tools + ReAct), OpenConnector integration (HTTP + MCP), and live-config CLI are wired; Docker-gated sandbox test remains

## Phase Tracker

Verified against the codebase on 2026-09-07 (`uv run pytest -q`: **427 passed** / 1 env-gated fail (Docker daemon); ruff **All checks passed**; mypy **strict clean, 62 files**).

| Phase | Status | Evidence |
| --- | --- | --- |
| 0 — Repository forensics & scaffold | ✅ Done | Inventory written, tooling green, baseline commit |
| 1 — Domain + persistence | ✅ Done | 22-table schema, migration `94be8eadb99f`, kill-9/restart tests |
| 2 — Event system | ✅ Done | EventBus (monotonic seq, redaction, replay, **idempotent dedupe**) + WS/SSE fanout with resume tests |
| 3 — API + permissions | ✅ Done | session-secret auth, `/api/v1` routers, PermissionGate, approvals + expiry sweeper, contract tests |
| 4 — Orchestrator + queue | ✅ Done + **live-verified** | supervisor DAG + RQ worker; E2E: out-of-process execution ✓, kill -9 → lease reaper recovery ✓, attempt double-increment fixed |
| 5 — Sandbox + workspace | ✅ Done | DockerSandbox + traversal-safe WorkspaceManager + `/exec`; daemon-gated tests |
| 6 — Browser + research | ✅ Core done | Playwright contexts + recording + citations (optional import) |
| 7 — Documents | ✅ Done | PPTX/DOCX/XLSX/PDF deterministic builders + artifacts API |
| 8 — Memory + vault | ✅ Core done | Obsidian writer + MemoryStore + hashed-lexical embeddings; LanceDB swap-in pending |
| 9 — Dashboard | ✅ Core done | Next.js 16 shell; **wired tabs:** Chat, Kanban, Approvals, Workspace, Outputs, Audit, Recipes, Insights, Cost, Reasoning/Trace, Schedule, Settings; **honest placeholders:** Vault, Templates (state what's implemented + pending) |
| 10 — CLI | ✅ Core done | `agentctl` (sessions/tasks/approvals/workspaces/events/status/chat), `--json`, exit codes, contract tests |
| 11 — Router + cost | ✅ Done | ModelRouter/Pricing/BudgetMonitor + `/model-calls` API + Cost tab |
| 12 — Error recovery | ✅ Done | classify→plan→execute→learn; no auto-retry on destructive/permission/validation |
| 13 — QA | ✅ Core done | untrusted-test sandbox + subprocess fallback; 1 test fails only w/o Docker daemon |
| 14 — Templates | ✅ Done | tar snapshots + pre-snapshot secret scan + clone fidelity |
| 15 — Recording + replay | ✅ Done | BehaviorRecorder (.jsonl, secret-scrubbed), RecordingContext, ReplayService **INSPECT/SIMULATE/APPROVED_REEXECUTE**, fingerprint diff, `recordings/` + `recordings/{id}/replay` API, 15 unit tests |
| 16 — Batching + recipes | ✅ Done | TaskBatcher (compatibility, partial-failure isolation, cancel, speedup), RecipeEngine (versioned validated DAGs, params, canonical pipeline, cancel), APIs + 19 tests |
| 17 — Personality + insights | ✅ Done | PersonalityManager (versioned, prompt-only learning at N=10, security fields rejected), InsightGenerator (event-derived, redacted, anomaly suggestions), `/personality` `/insights` `/schedule` APIs + tests |
| 18 — Autopilot | ✅ Done (service) | **Off by default**, per-action fresh APPROVED approval binding, hard default-deny scopes, action/time caps, sticky kill switch, full audit — 17 security tests |
| 19 — Hardening | ✅ Core done | chaos tests (event dedupe, double-delivery, crash-reopen, fresh-engine restart, approval expiry, replay-safety-after-mutation, batch partial failure) + FINAL_REPORT.md |
| 20 — Real LLM task execution | ✅ Done | `agents/react_agent.py` — `llm_react_handler` (ModelRouter + `run_tool_loop`, tools: shell/files/web/memory/MCP/OpenConnector); worker + orchestrator wiring; goal-aware fallback; 7 unit tests + E2E smoke |
| 21 — OpenConnector + MCP over HTTP | ✅ Done + **live-verified** | `services/openconnector.py` (real `/v1` Runtime API: execute/list/guides/health) + `services/mcp.py` streamable-HTTP MCP transport (SSE/JSON, `Mcp-Session-Id`) + implicit `openconnector` MCP server + `openconnector_execute/list` + `mcp_list` tools; verified against `ghcr.io/oomol-lab/open-connector:latest` (action exec + MCP `tools/call`); 12 unit tests |

## Tests Last Executed

- 2026-09-07: **427 passed, 1 failed (env-gated: Docker daemon)** (`uv run pytest -q`).
- ruff: All checks passed. mypy --strict: Success (62 files).
- The single failure (`test_real_docker_sandbox_runs_untrusted_test`) requires the Docker daemon.
- Worker E2E smoke (echo provider): task → ReAct loop → ModelRouter → `ModelCall` → events → SUCCEEDED.
- OpenConnector live verification: real container action discovery, `hackernews.get_top_stories` execution, action guide markdown, HTTP MCP `initialize → tools/list → tools/call` with session-id replay.

## Blockers (environment only — no code gaps)

- ~~Docker/Redis absent~~ → **Redis live-verified 2026-09-06** (`docker compose up -d`); Docker daemon still absent for sandbox/browser/QA-live paths.

## Live Worker Verification (2026-09-06)

- `docker compose up -d` → redis:7-alpine healthy; `redis-cli ping` → PONG.
- `python -m agent_system.worker` listens on queue `agent-system` (worker name now unique per host+process — a crashed worker's stale registration no longer blocks restart).
- **E2E happy path** (`scripts/e2e_worker_check.py`): session → task QUEUED → RQ enqueue → worker picked up → SQLite SUCCEEDED, attempt=1, AgentRun COMPLETED with `worker_id=agent-worker-pop-os-…` (proves out-of-process), lease deleted, events `task.created → queued → started → completed`.
- **E2E crash recovery** (`scripts/e2e_crash_recovery_check.py`): kill -9 while RUNNING → lease expired → `recover_orphans` reaped lease, RECOVERING→QUEUED, attempt stays 1 (double-increment bug fixed), no duplicate agent_runs, `recovery.started/completed` events emitted.
- Test expectations updated to the corrected invariant: `attempt` counts "times execution started" — incremented only at RUNNING transitions (worker, orchestrator, manual API transition), never on requeue.

## Next

1. Install Docker sandbox image deps → unblock sandbox/browser/QA live tests (Docker daemon itself is present now).
2. Vault + Templates tab APIs → convert the last two placeholder tabs.
3. Generic scheduler enable/trigger endpoints — `/schedule` CRUD APIs exist, and the nightly **backup** scheduler is already wired in `api/main.py:93-115` (`BackgroundScheduler`, `nightly-backup` cron from `backup_schedule_cron`, `scheduler_enabled` gate, SQLite-only).
4. Sub-agents spawn + todos checklist + memory hooks + vault API.
5. UI wiring for auth/skills/providers/settings/soul/vault/schedule-create views.
6. E2E acceptance run (master plan §7) after clean install + restart.
