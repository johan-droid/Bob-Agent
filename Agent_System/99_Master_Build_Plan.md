---
title: Master Build Plan — Start Here
type: plan
project: Agent System
status: active
spec_version: "3.0 ⊕ 3.1"
updated: 2026-09-06
up: "[[00_Index]]"
---

# 🚀 Master Build Plan — The One Document to Start the Job

> **Purpose:** This is the single, self-contained execution document for building the Local Autonomous AI Agent System. It merges the **best of v3.0** (product vision: features, UI/UX, CLI, schema, APIs) with the **best of v3.1** (engineering contract: domain model, events, lifecycles, security, reliability, testing).
>
> **How to use:** Work top-to-bottom. Execute phases in order. Never skip the [[#Phase Gate — DoD Checklist]] between phases. Deep detail for any section lives in the linked vault note; this file always tells you exactly what to *do next*.

---

## 0. Document Map — Where Truth Lives

| If you need… | Look at | This file covers |
| --- | --- | --- |
| The job list in order | **This document §5** | Full Phase 0–19 plan with work items + acceptance |
| Product feature detail | [[01_Overview]] · [[05_Feature_Reasoning_Trace_Viewer]] … [[15_Feature_Insight_Generation]] | Summary + v3.1 deltas per feature |
| Screen-by-screen UI | [[16_Dashboard_UIUX]] | Design tokens recap only |
| CLI look & feel | [[17_CLI_Specification]] | Command list recap |
| Exact DB schema | [[04_Data_Model]] | Entity list only |
| Security rules | [[27_Security_Permissions]] | Gate summary |
| Contract & policies | [[23_Engineering_Contract]] | Guardrails recap |
| Decisions made | [[22_Decision_Log]] | — |

**Source-of-truth hierarchy (v3.1 §2):** repository reality → v3.1 contract → v3.0 product reqs → tests → docs → assumptions. When this file and another note conflict, the **stricter/safer** rule wins.

---

## 1. What We're Building (One Paragraph)

A locally-hosted, persistent, multi-agent AI system: a supervisor decomposes user goals into a task DAG, executes them with isolated subagents (code, browser, research, documents, scheduler, autopilot) inside Docker sandboxes, every action flows through a canonical event bus and permission gate, all state survives crashes in SQLite, and the user watches and controls everything through a localhost web dashboard and a `agentctl` CLI — with cost tracking, memory (Obsidian + LanceDB), scheduled insights, and zero fake functionality.

## 2. v3.0 ⊕ v3.1 — Reconciliation (What We Kept, Changed, Dropped)

| Area | v3.0 said | v3.1 changed it to | We adopt |
| --- | --- | --- | --- |
| Reasoning trace | Token-by-token "reasoning" UI | **Decision & Execution Trace** — auditable events only, tokens optional | v3.1 (honest, provider-safe) |
| Model names | Hardcoded (`claude-opus-5`, …) | Provider adapters + external **ModelRegistry** config | v3.1 (v3.0 names become configurable placeholders) |
| Storage | Redis-centric queue state | **SQLite authoritative** (WAL/FK/busy-timeout); Redis = queue/cache only | v3.1 |
| API | Loose `/api/...` paths | **Versioned `/api/v1`**, documented schemas, pagination, idempotency | v3.1 (v3.0 endpoint surface kept) |
| Permissions | Approval queue UI | Formal gate: 4 risk levels × 5 policies, expiring records, audit | v3.1 (v3.0 UI kept) |
| Tasks | Ad-hoc status field | Explicit **10-state machine**, transitions validated + evented | v3.1 |
| Replay | Step-through viewer | **INSPECT / SIMULATE / APPROVED_REEXECUTE** with fingerprint checks | v3.1 (v3.0 UI kept) |
| QA tests | Auto-generated & run | Generated tests are **untrusted code** — sandboxed | v3.1 |
| Templates | Save/clone workspace | + **secret exclusion scan**, version metadata | both |
| Personality | Tone/verbosity learning | + never touches security/permissions/limits; versioned | v3.1 |
| Insights | Scheduled summaries | Derived **only from canonical events**, redacted, never fabricated | v3.1 |
| Cost | Daily budget | Provider-aware tokens (cached/estimated/unknown), 4 budget scopes, 4 alert levels | v3.1 |
| Phases | 16 phases | **20 phases**, gated | v3.1 (v3.0 work items folded in) |
| UI/UX design system | Full spec | Unchanged | v3.0 |
| CLI UX | Full spec | + shared API contracts, stable JSON/exit codes | both |
| Rust CLI | v3 aspiration | Deferred | v3.0-late |
| Feature count | 12 features | Same 12, each gated by real DoD | both |

---

## 3. Locked Foundations (Don't Relitigate)

### 3.1 Stack

| Layer | Choice |
| --- | --- |
| Backend | Python 3.12 + FastAPI (uv-managed) |
| Workers | RQ on Redis + process-isolated subagents (asyncio) |
| DB | SQLite (WAL, FK, busy_timeout) via SQLAlchemy + Alembic; Postgres optional later |
| Vectors | LanceDB (embedded) |
| Memory | Obsidian vault (`.md` + YAML frontmatter) |
| Frontend | Next.js 15 + TypeScript strict + Tailwind + shadcn/ui |
| Realtime | WebSocket + SSE (FastAPI native), sequence-resumable |
| CLI | Typer + Rich (`agentctl`) |
| Browser | Playwright (Python), recorded sessions |
| Docs gen | python-pptx, python-docx, weasyprint, openpyxl |
| Sandbox | Docker (code/browser/QA); restricted OS account (autopilot, Phase 18) |
| Scheduler | APScheduler + Redis jobstore |
| Ports | API `:8000` · Web `:3000` · Redis `:6379` |

### 3.2 Identifiers & Conventions

- Prefixed ULIDs everywhere: `ses_`, `task_`, `run_`, `evt_`, `approval_`, `ws_`, `artifact_`, `recording_`, `recipe_`, `qa_`, `insight_` ([[24_Canonical_Domain_Model]]).
- Timestamps UTC ISO-8601. JSON validated with Pydantic at boundaries.
- Trace keys in all logs: `request_id` · `session_id` · `task_id` · `agent_run_id`.
- Layers: `API → Application Services → Domain → Infrastructure`. Forbidden: agent→raw SQL, agent→host Docker socket, frontend→DB, feature→feature's private tables, CLI→duplicated logic.

### 3.3 Event Envelope (the system's bloodstream — [[25_Event_System]])

```json
{
  "event_id": "evt_...", "schema_version": 1,
  "session_id": "ses_...", "task_id": "task_...", "agent_run_id": "run_...",
  "sequence": 123, "timestamp": "2026-09-06T12:00:00.000Z",
  "type": "task.started", "actor": "supervisor",
  "payload": {}, "visibility": "user", "sensitivity": "normal"
}
```

Catalog (33 types): `session.created/completed` · `task.created/queued/started/completed/failed/cancelled/recovering/blocked_approval` · `agent.created/started/waiting_tool/waiting_approval/completed/failed/terminated` · `model.requested/completed/failed` · `tool.started/completed/failed` · `approval.requested/approved/denied/expired` · `workspace.created/modified/destroyed` · `artifact.created/deleted` · `qa.started/completed/failed` · `recovery.started/completed/failed` · `recipe.started/completed/failed` · `cost.recorded` · `insight.generated`.

### 3.4 Task State Machine (validate → persist → event → timestamp → actor → reason)

`PENDING → PLANNING → QUEUED → RUNNING → (BLOCKED_APPROVAL ⇄) → RECOVERING → … → SUCCEEDED | FAILED | CANCELLED` (+ `REVIEW`). Invalid transitions **raise**. Full rules: [[26_Task_Agent_Lifecycles]].

### 3.5 Non-Negotiable Guardrails ([[23_Engineering_Contract]])

1. **No fake production behavior** — UI/CLI/API only show what a real backend does.
2. **No shortcuts** — no commented-out tests, no swallowed exceptions, no TODO-instead-of-implementation.
3. **Security > convenience** — permission gate cannot be bypassed by any agent; secrets redacted at the event/log boundary.
4. **Recovery > assumptions** — every phase proves restart-safety before closing.
5. **Phase gate** — no advancing with failing critical tests, broken migrations, security violations, data corruption, or fake functionality.

---

## 4. Target Repository Structure (create in Phase 0)

```
agent-system/
├── backend/
│   ├── src/agent_system/
│   │   ├── api/v1/            # FastAPI routers, schemas, deps (auth, pagination)
│   │   ├── domain/            # entities, state machines, events, policies (pure)
│   │   ├── services/          # application services (supervisor, orchestrator, gate)
│   │   ├── infra/             # db (sqlalchemy/alembic), redis, lancedb, vault
│   │   ├── agents/            # code, browser, research, document, scheduler, qa
│   │   ├── features/          # cost optimizer, recovery, tracer, insights, batching
│   │   └── config.py          # pydantic-settings, resource limits
│   ├── tests/                 # unit / integration / contract / security / recovery
│   ├── alembic/               # migrations
│   └── pyproject.toml
├── web/                       # Next.js 15 app (one folder per dashboard tab)
├── cli/                       # agentctl (Typer)
├── docs/implementation/       # REPOSITORY_INVENTORY, ARCHITECTURE, API, EVENTS,
│                              # RECOVERY, SECURITY, THREAT_MODEL, DECISIONS, STATUS,
│                              # OPERATIONS, TROUBLESHOOTING, AGENT_STATE, FINAL_REPORT
├── workspaces/  templates/  recordings/  outputs/   # runtime data (gitignored)
├── docker-compose.yml         # redis (+ optional postgres)
├── .env.example
└── Makefile                   # make dev / test / lint / migrate / up
```

---

## 5. The Plan — Phases 0–19 (Work Items + Acceptance)

> Legend per phase: **Build** = concrete work items (v3.0 detail ⊕ v3.1 rules) · **Accept** = gate criteria · **Docs** = update before commit · **Detail** = vault note.

### Phase 0 — Repository Forensics & Scaffold *(start here)*

**Build:**
1. Create the repo skeleton exactly as §4 (git init, uv, Makefile, docker-compose with Redis, `.env.example` with all vars from [[20_Deployment]]).
2. Tooling: `ruff` + `mypy` + `pytest` + `pytest-asyncio`, pre-commit hooks, GitHub Actions (or local script) running lint+test.
3. Write `docs/implementation/REPOSITORY_INVENTORY.md` (if building inside an existing repo: audit modules, entry points, dependency graph, schema, routes, tests, tech debt, dangerous/broken functionality — v3.1 §4).
4. Write initial `AGENT_STATE.md` + `STATUS.md` (phase 0, blockers: none).
5. Empty `web/` and `cli/` packages with health-check smoke.

**Accept:** inventory exists · architecture map · dependency map · `make test` green baseline · first commit.
**Docs:** STATUS, AGENT_STATE, REPOSITORY_INVENTORY. **Detail:** [[30_Implementation_Docs]], [[19_Execution_Plan]].

### Phase 1 — Domain + Persistence

**Build:**
- 16 canonical entities with prefixed ULIDs ([[24_Canonical_Domain_Model]]).
- SQLite pragmas (WAL, foreign_keys=ON, busy_timeout=5000); SQLAlchemy models + **Alembic** migrations for all tables: v3.0 core set ([[04_Data_Model]]) **plus** v3.1 additions: `events`, `model_calls`, `tool_calls`, `agent_leases`, `idempotency_keys`.
- Repository layer (no raw SQL outside `infra/`).
- Migration test triple: fresh DB · upgrade existing · downgrade/roundtrip.

**Accept:** migrations pass · CRUD tests pass · **kill -9 mid-write → DB uncorrupted, state intact**.
**Docs:** ARCHITECTURE, OPERATIONS. **Detail:** [[04_Data_Model]], [[28_Reliability_Operations]].

### Phase 2 — Event System

**Build:**
- Append-only `events` store; per-session monotonic `sequence`; dedupe on `event_id`.
- Emitters wired into a single `EventBus` (no second bus, ever).
- WS + SSE fanout with heartbeat; `GET /api/v1/events?after_sequence=N` resume.
- Redaction filter at emit-time (secrets never enter the bus — [[27_Security_Permissions]]).

**Accept:** ordering/dedupe/replay tests · duplicate delivery handled · WS disconnect+resume loses nothing.
**Docs:** EVENTS. **Detail:** [[25_Event_System]], [[18_API_Reference]].

### Phase 3 — API v1 + Permissions

**Build:**
- FastAPI app: `/api/v1/health`, `/ready`, versioned routers; explicit session-secret auth; localhost bind; restricted CORS.
- **Permission Gate** service: risk `LOW/MEDIUM/HIGH/CRITICAL` × policy `ALLOW_ONCE/ALLOW_SESSION/ALLOW_WORKSPACE/ALLOW_ALWAYS/DENY`; expiring approvals persisted with full record ([[27_Security_Permissions]]); default-deny list (host FS, credentials, shell, payments, …).
- Endpoints for approvals: list/approve/deny/always-allow + expiry sweeper → `approval.expired`.
- Contract tests for every endpoint schema.

**Accept:** unauthorized rejected · approvals persisted & expiring · contract tests green · path-traversal & redaction security tests green.
**Docs:** API, SECURITY, THREAT_MODEL. **Detail:** [[18_API_Reference]], [[27_Security_Permissions]].

### Phase 4 — Orchestrator + Queue

**Build:**
- Supervisor: goal → task **DAG** (cycle detection, spawn/concurrency limits).
- Task state machine (§3.4) with transition table + `task.*` events.
- RQ workers; agent runs as isolated processes with **lease/heartbeat** ([[26_Task_Agent_Lifecycles]]); lease reaper → orphaned runs → `RECOVERING` or clean fail.
- Cancellation (cooperative, at safe points); idempotency keys on task creation/execution.
- **Worker-crash recovery:** restart → detect stale leases → resume safe tasks, fail unsafe ones with audit intact.

**Accept:** multi-step DAG completes · `kill -9` worker mid-task → system recovers without duplicates · no zombie RUNNING agents after 2× lease TTL.
**Docs:** RECOVERY, ARCHITECTURE. **Detail:** [[26_Task_Agent_Lifecycles]], [[28_Reliability_Operations]].

### Phase 5 — Sandbox + Workspace

**Build:**
- One Docker container per workspace; CPU/memory/pids limits; network policy; execution timeout.
- Workspace CRUD API + file tree; diff view data; safe archive extraction (zip-slip protection); file-size/output-size caps.
- Secret exclusion: `.env`/keys never copied into containers from host.

**Accept:** isolation tests (escape attempts fail) · limits enforced with evented stop · persistence across restarts.
**Detail:** [[19_Execution_Plan]] Ph5, [[27_Security_Permissions]].

### Phase 6 — Browser + Research

**Build:** Playwright contexts (isolated profile, no host keyrings), session recording (video+HAR), `browser:*` permission scopes, crash→recoverable-failure mapping, research agent with citation capture.
**Accept:** isolated sessions · permission checks fire · recording replayable · browser crash = clean `task.failed` + `recovery.*`.
**Detail:** [[19_Execution_Plan]] Ph6.

### Phase 7 — Documents

**Build:** DocumentAgent producing PPTX/DOCX/PDF/XLSX via template-based generation **inside sandbox**; artifacts persisted (`artifact.created`) with Outputs-tab API.
**Accept:** all 4 formats valid (open-test) · artifact persistence · sandboxed generation.
**Detail:** [[19_Execution_Plan]] Ph7.

### Phase 8 — Memory + Vault

**Build:**
- Obsidian writer: YAML frontmatter, `[[wiki-links]]`, attribution (source/task/session/agent/timestamp), folder conventions.
- LanceDB embedding + retrieval; memory layers SYSTEM/USER/TASK/WORKSPACE separated; secret filter before any write.
**Accept:** persistence · retrieval quality smoke · secrets never land in vault or vectors.
**Detail:** [[28_Reliability_Operations]] §Memory.

### Phase 9 — Dashboard

**Build:** Next.js 15 shell (dark sidebar, sticky header) + tabs in this order: Chat → Kanban → Approvals → Workspace → Vault → Outputs → Schedule → Cost → Reasoning(=Trace) → Templates → Recipes → Insights → Audit → Settings. Design tokens from [[16_Dashboard_UIUX]]. **Every control wired to `/api/v1`** — fixtures only behind a dev flag. WS clients implement reconnect + `after_sequence` resume.
**Accept:** UI smoke tests of critical journeys · zero mocked production paths · reconnect mid-stream loses nothing.
**Detail:** [[16_Dashboard_UIUX]], [[23_Engineering_Contract]] §Frontend Rule.

### Phase 10 — CLI

**Build:** `agentctl` (Typer+Rich): chat, status, agents, tasks, workspace, vault, documents, browser, autopilot, schedule, approvals, recipes, cost, insights, memory, logs, start/stop/restart, config, version. Same `/api/v1` client as web. `--json` (stable schema), `--verbose`, `--no-color`; documented exit codes.
**Accept:** every implemented backend operation reachable · JSON schema tests · exit-code tests.
**Detail:** [[17_CLI_Specification]].

### Phase 11 — Model Router + Cost

**Build:** ProviderRegistry/ModelRegistry/PricingRegistry/ModelSelector/CostPredictor — **config-driven, no hardcoded fictional IDs**. Every invocation → `model_calls` row. Provider-aware usage (input/output/cached/estimated/unknown; unknown never crashes). Budgets: task/session/daily/provider; alerts 50/75/90/100%; `cost.recorded` events; Cost tab + `agentctl cost` live.
**Accept:** per-task model selection logged with reason · estimated flags correct · budget alerts fire at all 4 levels.
**Detail:** [[06_Feature_Multi_Model_Orchestration]], [[10_Feature_Cost_Optimizer]].

### Phase 12 — Error Recovery

**Build:** ErrorIntrospector (11-class taxonomy), RecoveryPlanner (queries `failure_patterns`), RecoveryExecutor (adjusted retry), PatternLearner. Retry rules: never auto-retry destructive/permission/deterministic-validation/repeated-identical; max-retries + exponential backoff; everything emits `recovery.*`.
**Accept:** seeded failures recovered safely · non-retryable classes verified · patterns persisted and surfaced.
**Detail:** [[07_Feature_Error_Recovery]].

### Phase 13 — QA Agent

**Build:** TestGenerator → run generated tests as **untrusted code in sandbox** (no host FS/creds, restricted net, CPU/mem caps, timeout); CoverageAnalyzer; structured `qa_reports` (generated/executed/passed/failed/skipped/duration/coverage/diagnostics); QA tab.
**Accept:** sandbox escape attempts fail · reports complete · low-coverage flagging.
**Detail:** [[12_Feature_Autonomous_QA]].

### Phase 14 — Workspace Templates

**Build:** Snapshot (tar) with **pre-snapshot secret scan** (blocks `.env`, keys, tokens, browser sessions); version + source metadata; clone restores files/git/venv; Templates tab + API.
**Accept:** clone fidelity test · secret-exclusion security test · used_count/versioning correct.
**Detail:** [[08_Feature_Workspace_Templates]].

### Phase 15 — Recording + Replay

**Build:** BehaviorRecorder (`RecordingContext` wraps all agent I/O → `.jsonl`); Replay UI (scrubber, step-through, diff viewer); modes **INSPECT / SIMULATE / APPROVED_REEXECUTE** with pre-re-execute fingerprint comparison (workspace, OS, deps, agent version, model config, recipe version, permissions); risky re-execution requires fresh approval.
**Accept:** 3-mode boundary tests · mismatched fingerprint blocks without approval · recordings contain no secrets.
**Detail:** [[09_Feature_Behavior_Recording]].

### Phase 16 — Batching + Recipes

**Build:**
- TaskBatcher: compatibility check (agent type, workspace, task type, permissions, deps, resources) → `batch_id`, idempotency, cancellation, **partial failure** isolation, per-task results, speedup metric.
- Recipe system: versioned DAG JSON + params + validation; execution rides the canonical task/event pipeline; **never bypasses permissions**; history + cancellation.
**Accept:** partial-failure test · incompatible batch rejected · recipe re-run matches v3.0 one-click UX with v3.1 safety.
**Detail:** [[11_Feature_Task_Batching]], [[13_Feature_Recipe_Library]].

### Phase 17 — Personality + Insights

**Build:** Versioned personality configs (tone/verbosity/reasoning-style) + feedback loop (N=10 ratings → inference) — **cannot modify security/permission/safety/limits**; system-prompt injection audited. InsightGenerator: scheduled daily/weekly jobs reading **only canonical events**; redacted; anomalies with suggested actions; Insights tab.
**Accept:** personality versioned/audited · feedback adjusts prompts only · insights traceable to events, zero fabrication.
**Detail:** [[14_Feature_Personality]], [[15_Feature_Insight_Generation]].

### Phase 18 — Autopilot (LAST — gated)

**Build:** Desktop automation (accessibility tree + vision + pyautogui/pynput) under a **restricted OS account**, behind its own permission boundary, **off by default** (config flag). Every input action recorded + audited + approval-gated; hard execution-time caps.
**Accept:** disabled by default · cannot run without approvals · full audit trail · kill-switch works.
**Detail:** [[27_Security_Permissions]], [[19_Execution_Plan]] Ph18.

### Phase 19 — Production Hardening

**Build/Run:**
- **Chaos suite** ([[29_Testing_Strategy]]): kill worker/API/Redis/browser/container; LLM & network timeouts; DB lock; WS disconnect; duplicate event/task; approval expiry → predictable failure each time.
- Security audit vs THREAT_MODEL; migration tests (fresh+upgrade); performance pass (limits from §6); UI smoke; CLI tests; restart tests.
- Full **E2E acceptance** (§7 below) after clean install/restart.
- Documentation audit: every doc matches behavior; write `FINAL_REPORT.md` with honest labels (IMPLEMENTED / PARTIALLY IMPLEMENTED / CONFIGURATION REQUIRED / KNOWN LIMITATION / BLOCKED).
**Accept:** all checkboxes in §8 ticked.
**Detail:** [[29_Testing_Strategy]], [[30_Implementation_Docs]].

---

## 6. Feature → Phase Map (all 12 v3.0 features land here)

| # | Feature (v3.0) | Lands in | v3.1 delta |
| --- | --- | --- | --- |
| 1+11 | Reasoning Trace Viewer | Ph 2 (events) + Ph 9 (UI) | Auditable trace only; renamed Decision & Execution Trace |
| 2 | Multi-Model Orchestration | Ph 11 | Registry/config-driven, ModelCall records |
| 3 | Error Recovery | Ph 12 | 11-class taxonomy + no-auto-retry rules |
| 4 | Workspace Templates | Ph 14 | Secret scan + versioning |
| 5 | Behavior Recording & Replay | Ph 15 | 3 safe modes + fingerprints |
| 6 | Cost Optimizer | Ph 11 | Provider-aware, 4 scopes, unknown-safe |
| 7 | Task Batching | Ph 16 | Compatibility + partial failure |
| 8 | Autonomous QA | Ph 13 | Untrusted-code sandbox |
| 9 | Recipe Library | Ph 16 | Versioned, permission-bound |
| 10 | Personality | Ph 17 | Bounded, versioned, audited |
| 12 | Insight Generation | Ph 17 | Event-derived, redacted |

## 7. End-to-End Acceptance (the moment of truth)

Goal: *"Create a workspace, inspect the repository, identify failing tests, fix the failures, run QA, summarize the changes, and produce a report."*

```
Session → Task → Supervisor plans → Workspace → CodeAgent → Model selected
→ ModelCall logged → ToolCalls logged → Trace events emitted → Code changed
→ Failure detected → Recovery executed → QA sandbox run → Results recorded
→ Diff generated → Approval requested → Granted → Committed → Artifact created
→ Audit complete → Memory updated → Insight (if applicable) → Response returned
```

Run it **after a clean install and a full restart**. Any missing step = not done.

## 8. Final Validation Checklist

[ ] Fresh install · [ ] migrations · [ ] API starts · [ ] worker starts · [ ] Redis OK (and survives Redis loss) · [ ] frontend builds · [ ] CLI works · [ ] auth · [ ] permissions · [ ] tasks persist · [ ] tasks survive restart · [ ] agent lifecycle · [ ] event system · [ ] WS reconnect · [ ] SSE reconnect · [ ] sandbox · [ ] workspace isolation · [ ] browser agent · [ ] document generation · [ ] memory · [ ] scheduler · [ ] model router · [ ] cost tracking · [ ] error recovery · [ ] QA · [ ] templates · [ ] replay safety · [ ] batching · [ ] recipes · [ ] personality · [ ] insights · [ ] audit · [ ] autopilot gated · [ ] no secret leaks · [ ] security tests · [ ] chaos tests · [ ] migration tests · [ ] UI smoke · [ ] CLI tests · [ ] docs match implementation

---

## 9. ⚡ Kickoff — Do This Now (Phase 0 in ~10 commands)

```bash
# 1. Create the repo
mkdir agent-system && cd agent-system && git init
uv init && uv venv

# 2. Skeleton
mkdir -p backend/src/agent_system/{api/v1,domain,services,infra,agents,features} \
         backend/tests/{unit,integration,contract,security,recovery} \
         backend/alembic/versions web cli docs/implementation \
         workspaces templates recordings outputs

# 3. Tooling
uv add fastapi uvicorn[standard] sqlalchemy alembic pydantic-settings \
       redis rq python-ulid structlog
uv add --dev pytest pytest-asyncio ruff mypy httpx
npx create-next-app@latest web --typescript --tailwind --eslint --app
mkdir -p cli && touch cli/pyproject.toml

# 4. Infra
printf 'services:\n  redis:\n    image: redis:7-alpine\n    ports: ["6379:6379"]\n' > docker-compose.yml
cp .env.example .env   # fill every var from note 20_Deployment

# 5. Baseline + first commit
make lint test          # green on an empty suite = baseline
git add -A && git commit -m "phase0: scaffold, tooling, inventory baseline"

# 6. Start the inventory (if wrapping an existing repo) or ARCHITECTURE (if greenfield)
$EDITOR docs/implementation/REPOSITORY_INVENTORY.md
```

**Then:** write `AGENT_STATE.md` (phase=0, next=Phase 1), and begin Phase 1 with the entity list from [[24_Canonical_Domain_Model]] and the table DDL from [[04_Data_Model]].

---

## 10. Risk Register (top pitfalls & mitigations)

| Risk | Mitigation |
| --- | --- |
| SQLite lock contention under workers | WAL + busy_timeout + short transactions; single-writer service per table group |
| Redis loss losing task state | Redis never authoritative ([[28_Reliability_Operations]]); SQLite reconstructs queue |
| Secret leakage into events/UI/templates | Redaction at emit-time; pre-snapshot scans; security tests in every phase ≥3 |
| Unbounded agent spawning/cost | Concurrency + token/cost limits with evented stop; budget alerts at 4 levels |
| Zombie agents after crash | Leases + reaper (Phase 4, tested with kill -9) |
| Fake UI creeping in | Frontend rule + UI smoke tests assert live API data |
| Scope creep (12 features at once) | Strict phase order; feature map §6 says exactly when each lands |
| Autopilot disaster | Disabled by default; Phase 18 last; own boundary + kill switch |

---

## 11. Per-Phase DoD Template (copy into STATUS.md each phase)

```
Phase N — <name>                     Date: YYYY-MM-DD
[ ] Implementation     [ ] Integration      [ ] Persistence
[ ] Error handling     [ ] Security boundary [ ] API contract
[ ] UI/CLI wiring      [ ] Tests added       [ ] Restart verified
[ ] Docs updated       [ ] No fake behavior  [ ] Acceptance criteria met
Tests: X passed / Y failed   Blockers: <none | list>   Next: Phase N+1
```

---

*This plan compiles [[19_Execution_Plan]], [[23_Engineering_Contract]], and the v3.0 product spec into one file. Keep it open while building; update the checkbox culture, not the plan, when reality disagrees — and record why in [[22_Decision_Log]].*
