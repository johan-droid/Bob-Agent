# Repository Inventory

> **Date:** 2026-09-16 · **Phase:** 0 → 21 **+ reconciliation (R)** · **Type:** Greenfield scaffold (v3.1 §4)
> This file records the **as-built** state. See `STATUS.md` and
> `ARCHITECTURE_RECONCILIATION.md` for the phase-by-phase evidence and the full
> capability/event/agent inventories.

## Context

This project was scaffolded fresh inside `agent-system/`. The inventory below records the
as-built reality so later work can be checked against it.

## Current Structure

```
agent-system/
├── backend/
│   ├── src/agent_system/
│   │   ├── api/            # FastAPI app + v1 routers (sessions/tasks/approvals/workspaces/
│   │   │                   #   events/model_calls/recordings/batches/recipes/personality/
│   │   │                   #   insights/schedule/autopilot/tools/vault/templates/health)
│   │   ├── agents/         # definitions.py (9 declared agents) + registry.py (explicit
│   │   │                   #   resolution/fallback) + react_agent.py (llm_react_handler)
│   │   ├── cli/            # agentctl (setup wizard + chat REPL + subcommands)
│   │   ├── domain/         # ids, events (canonical catalog + extensions + validation),
│   │   │                   #   task/agent state machines (pure)
│   │   ├── services/       # planner, supervisor/orchestrator, budget, context, model_router,
│   │   │                   #   permissions (one gate), agent_loop, openconnector, mcp, memory,
│   │   │                   #   vault, obsidian, documents, browser, sandbox, qa, scheduler,
│   │   │                   #   replay, recipes, personality, insights, autopilot, pricing
│   │   │   └── tools/      # CAPABILITY LIBRARY: registry, schemas, execution, protocol,
│   │   │                   #   paths, optional (MCP/OpenConnector), builtin/{filesystem,
│   │   │                   #   coding, git, shell, browser, research, documents, memory,
│   │   │                   #   tasks, system}, plugins/manager
│   │   ├── infra/          # db engine+pragmas, ORM models (23 tables), EventBus, repositories
│   │   ├── migrations/     # Alembic (head: e6f7a8b9c0d1)
│   │   └── features/       # feature-gated capabilities
│   ├── tests/              # 605 passed, 0 failed, 9 skipped:
│   │                       #   unit | contract | integration | security | recovery
│   ├── docker-compose.yml  # redis:7-alpine + optional open-connector gateway:3000
│   ├── Dockerfile
│   └── pyproject.toml      # uv-managed, ruff+mypy(strict)+pytest configured
├── web/                    # Next.js 16.3 dashboard (17 routes; Vault+Templates placeholders)
├── cli/                    # agentctl entry (thin /api/v1 client, --json, exit codes)
├── docs/                   # ARCHITECTURE, CONFIGURATION, OPERATIONS, STACK, DEVELOPER_GUIDE,
│   │                       #   DECISIONS, HARDENING_PASS_REPORT, CLOUD_HEROKU
│   └── implementation/     # ARCHITECTURE, API, EVENTS, RECOVERY, SECURITY, THREAT_MODEL,
│                           #   TROUBLESHOOTING, DECISIONS, STATUS, AGENT_STATE,
│                           #   REPOSITORY_INVENTORY, FINAL_REPORT, ARCHITECTURE_RECONCILIATION
├── workspaces/ templates/ recordings/ outputs/ data/   # runtime dirs (gitignored contents)
├── scripts/                # e2e_worker_check.py, e2e_crash_recovery_check.py
├── Makefile                # install/dev/test/lint/typecheck/migrate/up/down/check/qa-sandbox-image
└── .env.example            # all v3.1 §30 limits + storage paths + budgets + provider keys
```

## Database Schema

Alembic head: **`e6f7a8b9c0d1`** (permission-gate persistence). The reconciliation added
no tables — it wired the existing `approvals` table into the gate.

**23 tables:** `sessions, tasks, agent_runs, agent_leases, approvals, workspaces, artifacts,
events, model_calls, tool_calls, idempotency_keys, workspace_templates,
behavior_recordings, cost_budget, task_batches, qa_reports, recipes, agent_personalities,
feedback_log, insights, scheduled_jobs, memory_notes, alembic_version`.

SQLite pragmas enforced per-connection: WAL, foreign_keys=ON, busy_timeout=5000,
synchronous=NORMAL.

## Test Baseline

| Suite | Status |
| --- | --- |
| unit (`tests/unit/`) | ✅ pass (incl. `test_react_agent.py`, `test_openconnector_mcp.py`, `test_cli_*`) |
| contract (`tests/contract/`) | ✅ api/v1 contract tests incl. `test_planning_api.py` |
| integration (`tests/integration/`) | ✅ `test_permission_path.py`, `test_canonical_events_and_capabilities.py`, `test_planner_budget_routing.py`, `test_worker.py` |
| security (`tests/security/`) | ✅ jail / gate bypass / fence injection fail closed |
| recovery (`tests/recovery/`) | ✅ restart, dedupe, lease, approval expiry |
| **Total** | **605 passed, 0 failed, 9 skipped** |

Lint: `ruff check` clean · `ruff format --check` clean (153 files) · `mypy --strict` clean
(96 files). Web: `npx tsc --noEmit` clean · `npm run build` clean (17 routes).

Skipped (honest, not failures): 5 Redis-backed worker tests, 3 optional-extra tests
(memory embeddings, telemetry), 1 by-design read-tier tier assertion.

## Entry Points

- API: `uv run uvicorn agent_system.api.main:app --port 8000`
- Worker: `uv run python -m agent_system.worker`
- CLI: `agentctl` (after `uv pip install -e cli/`)
- Migrations: `uv run alembic upgrade head`

## Known Gaps (planned, not debt)

- ~~Docker sandbox image deps~~ **DONE** — `make qa-sandbox-image` builds it, CI builds it
  before pytest, and `test_real_docker_sandbox_runs_untrusted_test` runs in a real container.
- **Browser live-capture** still needs Playwright installed (optional dependency).
- Vault/Templates list APIs → convert the last two placeholder tabs.
- APScheduler service + enable/trigger endpoints (schedule CRUD APIs exist).
- Sub-agents spawn + todos checklist + memory hooks + vault API.
- UI wiring for auth/skills/providers/settings/soul/vault/schedule-create views.
- LLM-based planning (planner is deterministic and records its strategy).
- Browser download/upload; PDF text extraction.
- Real LLM + OpenConnector persistent named connections need operator setup (keys in `.env`).

## Dangerous Functionality

- **Sandbox/QA:** untrusted test code never runs in-process — Docker sandbox preferred,
  resource-limited isolated subprocess fallback. The QA image is built from the in-repo
  `backend/docker/qa-sandbox.Dockerfile` (not pulled from a registry); a missing image
  fails closed with `SandboxUnavailableError` and never silently weakens isolation.
- **One execution path:** every capability goes through
  `services/tools/execution.py` → `require_capability` → `PermissionGate.authorize`;
  `destructive` capabilities are default-deny and not approvable.
- **OpenConnector:** all action execution validates `ConnectorError`/`success:false` and
  never auto-trusts remote output.
- **Autopilot:** off by default, per-action approvals, hard default-deny list, sticky kill
  switch — routes through the one PermissionGate.
