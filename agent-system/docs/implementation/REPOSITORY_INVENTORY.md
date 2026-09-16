# Repository Inventory

> **Date:** 2026-09-07 · **Phase:** 0 → 21 (complete) · **Type:** Greenfield scaffold (v3.1 §4)
> This file originally recorded the Phase-1 as-scaffolded structure. Updated 2026-09-07 to reflect the **as-built** state through Phase 21 (build complete; see `STATUS.md`/`FINAL_REPORT.md` for the phase-by-phase evidence).

## Context

This project was scaffolded fresh inside `agent-system/`. There is no pre-existing legacy code to audit; the inventory below records the as-built reality so later phases can be checked against it.

## Current Structure

```
agent-system/
├── backend/
│   ├── src/agent_system/
│   │   ├── api/            # FastAPI app + v1 routers (sessions/tasks/approvals/workspaces/
│   │   │                   #   events/model_calls/recordings/batches/recipes/personality/insights/
│   │   │                   #   schedule/autopilot/tools/health)
│   │   ├── agents/         # react_agent.py (llm_react_handler, run_tool_loop)
│   │   ├── cli/            # agentctl (setup wizard + chat REPL + subcommands), settings wizard
│   │   ├── domain/         # ids, events envelope, task/agent state machines (pure)
│   │   ├── services/       # supervisor, worker, orchestrator, model_router, openconnector,
│   │   │                   #   mcp, tools, memory, vault, obsidian, documents, browser,
│   │   │       │     #   sandbox, qa, scheduler(plan), replay, recipes, personality, insights,
│   │   │   #   autopilot, pricing, budget
│   │   ├── infra/          # db engine+pragmas, ORM models (22 tables), EventBus, repositories
│   │   ├── migrations/     # Alembic (head: c3d4e5f6a7b8; chain: 94be8eadb99f → 7c1a2d9e4f50 → c3d4e5f6a7b8)
│   │   └── features/       # feature-gated capabilities (Phase 11+)
│   ├── tests/              # 427 passed, 1 env-gated fail (Docker daemon):
│   │                       #   unit | contract | integration | recovery | phase19 chaos
│   ├── docker-compose.yml  # redis:7-alpine + optional open-connector gateway:3000
│   ├── Dockerfile
│   └── pyproject.toml      # uv-managed, ruff+mypy(strict)+pytest configured
├── web/                    # Next.js 16.3 dashboard (14 tabs; 12 wired, Vault+Templates placeholders)
├── cli/                    # agentctl entry (thin /api/v1 client, --json, exit codes)
├── docs/                   # ARCHITECTURE, CONFIGURATION, OPERATIONS, STACK, DEVELOPER_GUIDE, DECISIONS, HARDENING_PASS_REPORT, CLOUD_HEROKU, implementation/*
├── workspaces/ templates/ recordings/ outputs/ data/   # runtime dirs (gitignored contents)
├── scripts/                # e2e_worker_check.py, e2e_crash_recovery_check.py
├── Makefile                # install/dev/test/lint/typecheck/migrate/up/down/check
├── .env.example            # all v3.1 §30 limits + storage paths + budgets + provider keys
└── .gitignore              # DBs, secrets, runtime data excluded
```

> Note: the `backend/src/agent_system/` package hosts the services; `cli/` holds the published `agentctl` shim. (Inventory historically duplicated the path — kept here for reference.)

## Database Schema (Phase 1 base `94be8eadb99f`, additions `7c1a2d9e4f50`, head `c3d4e5f6a7b8`)

23 tables: `sessions, tasks, agent_runs, agent_leases, approvals, workspaces, artifacts, events, model_calls, tool_calls, idempotency_keys, workspace_templates, behavior_recordings, cost_budget, task_batches, qa_reports, recipes, agent_personalities, feedback_log, insights, scheduled_jobs, memory_notes, alembic_version`.

SQLite pragmas enforced per-connection: WAL, foreign_keys=ON, busy_timeout=5000, synchronous=NORMAL.

## Test Baseline

| Suite | Count | Status |
| --- | --- | --- |
| unit (`tests/unit/`) | ~380+ | ✅ pass (incl. `test_openconnector_mcp.py` 12, `test_react_agent.py` 7, `test_cli_*` 52) |
| integration | — | ✅ full-stack goal → task → ReAct → model call → events |
| contract | — | ✅ api/v1 contract tests for sessions/tasks/approvals/workspaces/events |
| recovery (`tests/recovery/`) | — | ✅ Phase-19 chaos suite (dedupe, replay, restart, approvals, batch) |
| **Total** | **427 passed, 1 failed** | the 1 failure is env-gated (Docker daemon) |

Lint: `ruff check` clean · `ruff format` clean · `mypy --strict` clean (62 files). `npm run build` (web) clean.

## Entry Points

- API: `uv run uvicorn agent_system.api.main:app --port 8000`
- Worker: `uv run python -m agent_system.services.worker`
- CLI: `agentctl` (after `uv pip install -e cli/`)
- Migrations: `uv run alembic upgrade head`

## Known Gaps (planned, not debt)

Everything below is *scheduled* future work — the Phase-0→21 build is complete:

- **Phase 22+** scheduler runtime wiring (APScheduler service + enable/trigger) — `/schedule` APIs exist.
- Vault/Templates list APIs (`/api/v1/vault/notes`, `/api/v1/templates`) → last two placeholder tabs.
- Sub-agents spawn + todos checklist + memory hooks + vault API.
- UI: auth, skills, providers, settings, soul, vault, schedule-create views (APIs support them; wiring pending).
- Real LLM + OpenConnector persistent named connections need operator setup (keys in `.env`).

## Dangerous Functionality

- **Sandbox/QA:** untrusted test code never runs in-process — Docker sandbox preferred, resource-limited isolated subprocess fallback (Phase 13).
- **OpenConnector:** all action execution validates `ConnectorError`/`success:false` and never auto-trusts remote output (Phase 21).
- **Autopilot:** off by default, per-action approvals, hard default-deny list, sticky kill switch — routes through PermissionGate (Phase 18).
