# Bob Agent — Complete Project Documentation

> Auto-compiled master document. All sections are sourced from the `documentations/` folder.

---


---

---
title: Local Autonomous AI Agent System — Index
type: moc
project: Agent System
spec_version: "3.1"
status: planning
updated: 2026-09-06
---

# 🤖 Local Autonomous AI Agent System — Index (MOC)

> [!info] Project Hub
> A locally-hosted, persistent, multi-agent AI system. Owner **Ashutosh**.
> **v3.0** = product vision · **v3.1** = engineering contract (2026-09-06).
> Start here, then follow the links below.

## 📌 Quick Links

| Note | Purpose |
| --- | --- |
| **[[99_Master_Build_Plan]]** | ⭐ **START HERE — single consolidated execution doc: v3.0 ⊕ v3.1, all phases, kickoff commands** |
| [[01_Overview]] | System summary, core capabilities, competitive features, non-goals |
| [[02_Architecture]] | Layer diagram, orchestrator core, subagent pool, runtime topology |
| [[03_Tech_Stack]] | Locked stack decisions with rationale |
| [[04_Data_Model]] | All database tables (core + feature tables), SQL schema |
| [[05_Feature_Reasoning_Trace_Viewer]] | Feature 1 & 11 — live reasoning tree |
| [[06_Feature_Multi_Model_Orchestration]] | Feature 2 — per-task LLM routing |
| [[07_Feature_Error_Recovery]] | Feature 3 — self-healing agent pipeline |
| [[08_Feature_Workspace_Templates]] | Feature 4 — snapshot & clone workspaces |
| [[09_Feature_Behavior_Recording]] | Feature 5 — record & replay agent actions |
| [[10_Feature_Cost_Optimizer]] | Feature 6 — budgets, alerts, recommendations |
| [[11_Feature_Task_Batching]] | Feature 7 — intelligent batch grouping |
| [[12_Feature_Autonomous_QA]] | Feature 8 — generated tests & coverage |
| [[13_Feature_Recipe_Library]] | Feature 9 — reusable workflow DAGs |
| [[14_Feature_Personality]] | Feature 10 — tone & verbosity adaptation |
| [[15_Feature_Insight_Generation]] | Feature 12 — scheduled briefings & anomalies |
| [[16_Dashboard_UIUX]] | Full web dashboard spec — design tokens, all 11+ tabs |
| [[17_CLI_Specification]] | `agentctl` commands, TUI outputs, scripting flags |
| [[18_API_Reference]] | REST + WS endpoints grouped by domain |
| [[19_Execution_Plan]] | Phase 0–19 build order with acceptance criteria (v3.1) |
| [[20_Deployment]] | Install, services, env, service management |
| [[21_Progress_Log]] | Living progress & decision log |
| [[22_Decision_Log]] | Architecture decision records (ADRs) |

### v3.1 Engineering Contract

| Note | Purpose |
| --- | --- |
| [[23_Engineering_Contract]] | v3.1 contract: hierarchy, autonomy policy, phase gate, DoD, no-shortcuts |
| [[24_Canonical_Domain_Model]] | Entities, stable identifiers, layered architecture rules |
| [[25_Event_System]] | Event envelope, canonical catalog, realtime WS/SSE contract |
| [[26_Task_Agent_Lifecycles]] | Task & agent state machines, supervisor rules |
| [[27_Security_Permissions]] | Permission gate, risk levels, dangerous ops, secret handling |
| [[28_Reliability_Operations]] | Idempotency, crash recovery, SQLite/Redis split, limits, degraded modes |
| [[29_Testing_Strategy]] | Test pyramid, chaos testing, E2E acceptance lifecycle |
| [[30_Implementation_Docs]] | Required `docs/implementation/` files & validation checklist |

## 🎯 Core Capabilities (at a glance)

- CLI + ChatGPT-style web dashboard (localhost)
- Sandboxed coding workspace with live diff/approval
- Supervisor-based task decomposition and subagent orchestration
- Persistent memory: vector DB + Obsidian vault
- Research & browser automation with session recording
- Document generation (PPTX/PDF/DOCX/XLSX)
- Autopilot (desktop control via accessibility tree + vision + input control)
- Scheduler (cron/interval/date/webhook)
- Permission & trust model with approval queues

## ✅ Non-Goals (v1–v3)

Multi-tenant, cloud, billing, mobile. Multi-device sync is a stretch goal.

## 🚦 Project Status

- **Phase:** Documentation / pre-build (spec v3.1 baseline complete)
- **Next action:** Execute [[19_Execution_Plan]] Phase 0 — Repository Forensics
- **Contract:** [[23_Engineering_Contract]] governs all implementation


---

---
title: Overview
type: spec
project: Agent System
status: locked
updated: 2026-09-06
up: "[[00_Index]]"
---

# 1. System Overview

> **Source:** Spec v3.0 §1 · **Status:** Locked (product vision)

> [!warning] v3.1 Supersedes Where Applicable
> This note is the v3.0 product vision. Implementation is governed by the **v3.1 engineering contract** ([[23_Engineering_Contract]]). Where they conflict, v3.1 wins — see [[24_Canonical_Domain_Model]], [[25_Event_System]], [[26_Task_Agent_Lifecycles]], [[27_Security_Permissions]], [[28_Reliability_Operations]].

## Summary

A locally-hosted, persistent, multi-agent AI system delivering autonomous task execution with human approval gates.

## Core Capabilities

1. **CLI + ChatGPT-style web dashboard** (localhost).
2. **Sandboxed coding workspace** with live diff/approval.
3. **Supervisor-based task decomposition** and subagent orchestration.
4. **Persistent memory:** vector DB + Obsidian vault.
5. **Research & browser automation** with session recording.
6. **Document generation** (PPTX/PDF/DOCX/XLSX).
7. **Autopilot** (desktop control via accessibility tree + vision + input control).
8. **Scheduler** (cron/interval/date/webhook).
9. **Permission & trust model** with approval queues.

## Unique Competitive Features (v3)

| # | Feature | Note |
| --- | --- | --- |
| 1 | Agent Reasoning Trace Viewer — live token-by-token LLM output with decision tree visualization | [[05_Feature_Reasoning_Trace_Viewer]] |
| 2 | Multi-Model Orchestration — per-task LLM selection | [[06_Feature_Multi_Model_Orchestration]] |
| 3 | Autonomous Error Recovery — introspect, propose fix, learn patterns | [[07_Feature_Error_Recovery]] |
| 4 | Workspace Templates & Cloning — snapshot/replay workspace state | [[08_Feature_Workspace_Templates]] |
| 5 | Agent Behavior Recording & Replay — timestamped replayable action logs | [[09_Feature_Behavior_Recording]] |
| 6 | Real-Time Cost Optimizer — token tracking, alerts, model selection | [[10_Feature_Cost_Optimizer]] |
| 7 | Intelligent Task Batching — auto-combine similar tasks | [[11_Feature_Task_Batching]] |
| 8 | Autonomous QA & Testing — generated test suites, self-testing | [[12_Feature_Autonomous_QA]] |
| 9 | Workflow Recipe Library — compose & reuse multi-agent workflows | [[13_Feature_Recipe_Library]] |
| 10 | Agent Personality & Tone Adaptation — learned preferences | [[14_Feature_Personality]] |
| 11 | Live Reasoning Visualization — real-time reasoning tree rendering | [[05_Feature_Reasoning_Trace_Viewer]] |
| 12 | Autonomous Insight Generation — scheduled summaries & anomaly alerts | [[15_Feature_Insight_Generation]] |

## Non-Goals (v1–v3)

- Multi-tenant
- Cloud deployment
- Billing
- Mobile
- Multi-device sync (stretch goal only)


---

---
title: Architecture
type: spec
project: Agent System
status: locked
updated: 2026-09-06
up: "[[00_Index]]"
---

# 2. Architecture Overview

> **Source:** Spec v3.0 §2

## Layer Diagram

```
┌────────────────────────────────────────────────────────────────────────┐
│                           USER-FACING LAYER                           │
│  ┌──────────────┐       ┌────────────────────────────────────────────┐ │
│  │     CLI      │       │     Web Dashboard (Next.js, localhost)      │ │
│  │  (Typer/Rich)│       │  Chat | Reasoning | Kanban | Workspace      │ │
│  │              │       │  Vault | Outputs | Schedule | Approvals     │ │
│  │              │       │  Templates | Cost | Recipes | Audit         │ │
│  └──────┬───────┘       └──────────────┬──────────────────────────────┘ │
└─────────┼──────────────────────────────┼────────────────────────────────┘
          │ HTTP/WS                      │ HTTP/WS/SSE
          ▼                              ▼
┌────────────────────────────────────────────────────────────────────────┐
│                  ORCHESTRATOR CORE (FastAPI + Redis)                   │
│ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌────────────────┐ │
│ │ Supervisor   │ │ Task Queue   │ │ LLM Router   │ │ Permission     │ │
│ │ (planner)    │ │ (Redis/RQ)   │ │ (multi-model)│ │ Gate           │ │
│ └──────┬───────┘ └──────┬───────┘ └──────┬───────┘ └────────┬───────┘ │
│        │                 │                │                 │          │
│ ┌──────▼─────────────────▼────────────────▼─────────────────▼───────┐ │
│ │              FEATURE PIPELINE (data flowing through)              │ │
│ │ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐              │ │
│ │ │ Cost         │ │ Error        │ │ Reasoning    │              │ │
│ │ │ Optimizer    │ │ Recovery     │ │ Tracer       │              │ │
│ │ └──────────────┘ └──────────────┘ └──────────────┘              │ │
│ └──────────────────────────────────────────────────────────────────┘ │
│                                                                        │
│ ┌──────────────────────────────────────────────────────────────────┐ │
│ │                     SUBAGENT POOL (isolated processes)             │ │
│ │ CodeAgent | BrowserAgent | DocumentAgent | ResearchAgent |         │ │
│ │ SchedulerAgent | AutopilotAgent | CustomAgent                      │ │
│ └──────┬───────────┬───────────┬───────────┬───────────┬────────────┘ │
│        │           │           │           │           │              │
│ ┌──────▼─┐ ┌──────▼──┐ ┌─────▼───┐ ┌────▼──────┐ ┌───▼──────────┐   │
│ │Coding   │ │Browser  │ │Document │ │Memory     │ │Autopilot     │   │
│ │Workspace│ │Runtime  │ │Gen      │ │System     │ │Runtime       │   │
│ │(Docker) │ │(Play-   │ │(pptx/   │ │(LanceDB + │ │(accessibility│   │
│ │         │ │wright)  │ │pdf/docx)│ │Obsidian)  │ │tree + vision)│   │
│ └─────────┘ └─────────┘ └─────────┘ └───────────┘ └──────────────┘   │
│                                                                        │
│ ┌──────────────────────────────────────────────────────────────────┐ │
│ │ Scheduler | Error Recovery Engine | Insight Generator             │ │
│ └──────────────────────────────────────────────────────────────────┘ │
│                                                                        │
│ ┌──────────────────────────────────────────────────────────────────┐ │
│ │ Audit Log | Session Recorder | Cost Tracker | Behavior Recorder   │ │
│ └──────────────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────────────┘
```

## Component Responsibilities

| Component | Responsibility |
| --- | --- |
| **Supervisor** | Decomposes user goals into a task DAG, assigns agent types, monitors completion |
| **Task Queue** | Redis + RQ; holds pending/running tasks with retry semantics |
| **LLM Router** | Per-task model selection (see [[06_Feature_Multi_Model_Orchestration]]) |
| **Permission Gate** | Intercepts risky actions, routes to approval queue |
| **Feature Pipeline** | Cross-cutting data flow: cost, error recovery, reasoning traces |
| **Subagent Pool** | Isolated OS processes per agent; crash-safe |
| **Audit/SRecorder/Tracker** | Persistence of every action for replay & compliance |

## Isolation Boundaries

| Runtime | Isolation mechanism |
| --- | --- |
| Coding workspace | Docker container per workspace |
| Browser automation | Playwright + Docker (recording enabled) |
| Autopilot | Restricted OS account + accessibility tree + vision |
| Subagents | Process isolation + asyncio |

## Key Data Flows

1. **Goal → Tasks:** CLI/Web → Supervisor → task DAG → Task Queue → Subagent Pool.
2. **Reasoning:** LLM tokens → Reasoning Tracer → WS → Dashboard tree (see [[05_Feature_Reasoning_Trace_Viewer]]).
3. **Approvals:** Agent action → Permission Gate → approval queue → user decision → resume.
4. **Memory:** Results → vector DB (LanceDB) + Obsidian vault notes.

## Related

- Stack details: [[03_Tech_Stack]]
- Data model: [[04_Data_Model]]


---

---
title: Tech Stack
type: spec
project: Agent System
status: locked
updated: 2026-09-06
up: "[[00_Index]]"
---

# 3. Tech Stack (Locked)

> **Source:** Spec v3.0 §3 · Decisions in this table are locked; changes require an ADR in [[22_Decision_Log]].

| Layer | Tech | Why |
| --- | --- | --- |
| Backend | Python 3.12 + FastAPI | async, rich ecosystem |
| Subagents | Process isolation + asyncio | crash safety |
| CLI | Typer + Rich (v1); Rust binary (v3) | beautiful terminal output, clean UX |
| Frontend | Next.js 15 + Tailwind + shadcn/ui | familiar stack, component library ready |
| Realtime | WebSocket + SSE (FastAPI native) | token streaming, live updates |
| Task queue | Redis + RQ | proven, simple |
| Scheduler | APScheduler + Redis jobstore | cron/interval/date/webhook |
| Storage (relational) | SQLite (local) → Postgres (optional) | zero-config first |
| Storage (vector) | LanceDB (embedded) | no external server |
| Memory (human) | Obsidian vault: `.md` + YAML frontmatter | dual-use: agent + human readable |
| Browser | Playwright (Python) | session persistence, video recording |
| Document gen | python-pptx, python-docx, weasyprint, openpyxl | battle-tested, template-based |
| Computer-use | pyautogui, pynput, pytesseract, accessibility APIs | pixel + tree targeting fallback |
| Sandboxing | Docker (code, browser) + restricted OS account (Autopilot) | strong isolation boundaries |
| Packaging | uv + PyInstaller | one-command install |
| Service mgmt | systemd (Linux), launchd (macOS), NSSM (Windows) | survive reboot |

## Conventions

- Python: `ruff` + `mypy`, async-first modules under `core/`.
- Frontend: TypeScript strict, components under `web/components/`, one folder per dashboard tab.
- Config: single `.env` + `config/*.json` (e.g. `model_selection_rules.json`).
- Ports: API `:8000`, Dashboard `:3000`, Redis `:6379`.

## Related

- Deployment commands: [[20_Deployment]]
- UI design tokens: [[16_Dashboard_UIUX]]


---

---
title: Data Model
type: spec
project: Agent System
status: locked
updated: 2026-09-06
up: "[[00_Index]]"
---

# 7. Data Model (Extended)

> **Source:** Spec v3.0 §7 · SQLite first; Alembic-managed migrations.
>
> [!warning] v3.1 Storage Architecture
> Per [[28_Reliability_Operations]]: **SQLite is the authoritative durable store** (WAL, foreign keys, busy timeout, transactions, integrity checks). **Redis is never authoritative** — queue/coordination/cache/ephemeral only. Entities and identifiers are defined in [[24_Canonical_Domain_Model]]; tables below persist those entities. New entities without a v3.0 table (`events`, `tool_calls`, `model_calls`) are added via Alembic migration, reusing canonical IDs.

## Core Tables (from v2)

| Table | Purpose |
| --- | --- |
| `sessions` | User goal sessions (id, goal, status, created_at) |
| `tasks` | Task DAG nodes (id, session_id, type, input_json, status, depends_on_json, agent_type) |
| `agents` | Registered agent instances (id, type, status, pid) |
| `workspaces` | Coding workspaces (id, name, container_id, status, size_bytes, last_modified) |
| `approvals` | Pending/approved permission requests (action, scope, context_json, decision) |
| `browser_sessions` | Playwright session metadata + recording path |
| `scheduled_jobs` | APScheduler job definitions (cron/interval/date/webhook) |
| `audit_log` | Actor, action, scope, approval source, outcome, timestamp |
| `llm_usage_log` | Every LLM call (tokens in/out, model, latency) — extended per [[28_Reliability_Operations]] §Cost: cached tokens, estimated/unknown usage marked |
| `vault_notes` | Index of Obsidian notes (path, tags, embeddings ref) — memory layers per [[28_Reliability_Operations]] §Memory |
| `outputs` | Generated documents (path, type, task_id, size) |

## Feature Tables (v3)

```sql
-- Feature 1: Reasoning Traces ([[05_Feature_Reasoning_Trace_Viewer]])
reasoning_traces(
  id, session_id, agent_id,
  tokens_json,          -- list of {token, logprob, timestamp}
  decision_tree_json,   -- hierarchical decision nodes
  final_action,
  latency_ms, created_at
)

-- Feature 2: Model Selection Log ([[06_Feature_Multi_Model_Orchestration]])
model_selection_log(
  id, task_id,
  candidate_models,     -- json list
  selected_model,
  decision_reason,
  cost_estimate_usd, actual_cost_usd,
  latency_ms, created_at
)

-- Feature 3: Failure Patterns ([[07_Feature_Error_Recovery]])
failure_patterns(
  id, error_type, agent_type,
  root_cause_hypothesis_json,
  recovery_attempts,
  success_rate,
  preventive_action_json,
  last_seen
)
recovery_log(
  id, task_id,
  error_classification,
  original_error_msg,
  fix_proposed, fix_attempted, success,
  latency_impact_ms, created_at
)

-- Feature 4: Workspace Templates ([[08_Feature_Workspace_Templates]])
workspace_templates(
  id, name, description,
  source_workspace_id,
  snapshot_path,        -- tar.gz in templates/
  git_history_json,     -- commits from source
  tags_json,
  created_at, used_count
)
-- workspace_sessions (extended): template_id (nullable), cloned_from_template (boolean)

-- Feature 5: Behavior Recordings ([[09_Feature_Behavior_Recording]])
behavior_recordings(
  id, session_id, agent_id,
  recording_start, recording_end,
  action_count,
  action_log_path       -- jsonl file in recordings/
)

-- Feature 6: Cost Budget ([[10_Feature_Cost_Optimizer]])
cost_budget(
  id,
  period,               -- daily|weekly|monthly
  limit_usd,
  current_period_start,
  alert_threshold_pct,  -- alert at 80%
  created_at, updated_at
)
-- llm_usage_log (extended): model_selected, cost_usd, latency_ms, tokens_in, tokens_out

-- Feature 7: Task Batches ([[11_Feature_Task_Batching]])
task_batches(
  id,
  batch_type,           -- "code_review", "research", etc.
  task_ids_json,        -- list of original task IDs
  member_count,
  created_at, completed_at,
  speedup_factor        -- ratio of sequential vs. batch time
)

-- Feature 8: QA Reports ([[12_Feature_Autonomous_QA]])
qa_reports(
  id, task_id, code_file,
  tests_generated, tests_passed, tests_failed,
  coverage_pct,
  coverage_report_html,
  created_at
)

-- Feature 9: Recipes ([[13_Feature_Recipe_Library]])
recipes(
  id, name, description,
  task_dag_json,        -- task graph definition
  parameters_json,      -- {param_name: default_value}
  tags_json,
  created_at, executions_count
)

-- Feature 10: Agent Personalities ([[14_Feature_Personality]])
agent_personalities(
  id, agent_id,
  tone,                 -- "formal", "casual", "terse", "verbose"
  verbosity,            -- 1–10 scale
  reasoning_style,      -- "fast", "careful", "socratic"
  system_prompt_override,
  learned_from_feedback_count,
  updated_at
)
feedback_log(id, agent_id, session_id, rating, comment, created_at)

-- Feature 12: Insights ([[15_Feature_Insight_Generation]])
insights(
  id,
  insight_type,         -- "daily_briefing", "weekly_summary", "anomaly", "trend"
  generated_at,
  content_html,
  key_findings_json,
  archived_at
)
```

## v3.1 Additions (via migration)

New tables required by the canonical model ([[24_Canonical_Domain_Model]]) and event system ([[25_Event_System]]) — added in Phase 1–2, never created ad hoc:

```sql
-- Canonical events (append-only)
events(
  event_id, schema_version, session_id, task_id, agent_run_id,
  sequence, timestamp, type, actor, payload_json,
  visibility, sensitivity
)

-- Every LLM invocation ([[06_Feature_Multi_Model_Orchestration]])
model_calls(
  model_call_id, task_id, agent_run_id, provider, model_id,
  requested_at, completed_at, status,
  tokens_in, tokens_out, tokens_cached, usage_is_estimated,
  cost_usd, cost_is_estimated, latency_ms, error_json
)

-- Every tool invocation
tool_calls(
  tool_call_id, agent_run_id, task_id, tool_name,
  started_at, completed_at, status, risk, approval_id, result_json
)

-- Agent lease/heartbeat tracking ([[26_Task_Agent_Lifecycles]])
agent_leases(
  agent_run_id, worker_id, lease_expires_at, heartbeat_at, state
)

-- Idempotency keys ([[28_Reliability_Operations]])
idempotency_keys(
  key, operation, created_at, result_ref
)
```

## Conventions

- All IDs: prefixed ULID strings per [[24_Canonical_Domain_Model]], not autoincrement (safe across processes).
- Timestamps: UTC ISO-8601.
- JSON columns validated with Pydantic models at the API boundary.
- Vault notes mirror decisions in [[22_Decision_Log]].


---

---
title: Feature — Agent Reasoning Trace Viewer & Live Reasoning Visualization
type: feature
project: Agent System
status: spec
updated: 2026-09-06
feature_no: 1 + 11
up: "[[00_Index]]"
---

# Features 1 & 11 — Decision & Execution Trace (Agent Reasoning Trace Viewer)

> **Source:** Spec v3.0 §4 (Features 1 & 11) · **Size:** Medium
>
> [!warning] v3.1 Refinement (§18) — Consolidates Features 1 & 11
> Canonical name: **Decision & Execution Trace**. Do NOT attempt to expose private hidden chain-of-thought. Capture **auditable** information only:
> task goal · plan summary · decision summary · tool calls & results · model metadata · state transitions · approvals · failures · recovery decisions · final action.
>
> Token-level data is **optional and provider-dependent** — never claim to expose private internal reasoning the model/provider does not provide. The trace UI renders canonical events ([[25_Event_System]]); secrets redacted per [[27_Security_Permissions]].

## What it does

Real-time visualization of the agent's internal decision-making as it unfolds. User sees every token the LLM generates, organized into a decision tree, with branching showing when the agent considers alternatives.

Feature 11 deepens this with animation and interaction: reasoning tree rendered in real time as the agent thinks, with decision branches, explored paths, and choice rationale.

## Execution

```
core/features/reasoning_tracer.py
├── TokenStreamCapture   (intercepts LLM output token by token)
├── ReasoningTreeBuilder (parses tokens into a tree structure)
└── ReasoningRenderer    (converts tree to JSON for frontend visualization)

web/components/ReasoningTree.tsx
├── D3 tree layout + animated transitions
├── Real-time node injection as tokens arrive
└── Interactive node inspection (click to see full token sequence for that branch)
```

## Database

```sql
reasoning_traces(
  id, session_id, agent_id, timestamp,
  tokens_json,         -- list of {token, logprob, timestamp}
  decision_tree_json,  -- hierarchical decision nodes
  final_action,
  latency_ms
)
```

See [[04_Data_Model]].

## Backend behavior (Feature 11)

- Emit reasoning events via WebSocket as the LLM generates tokens.
- Parser detects reasoning markers (e.g., "Let me think..." or structured CoT format).
- Convert to tree nodes on the fly.

## Animation flow

- New reasoning step arrives via WebSocket: `{type: "decision", branch_id, text, confidence}`.
- React adds node to tree, animates entrance (fade + slide).
- User clicks node: sidebar shows token sequence for that branch, logprobs, time spent.
- User can "rewind" to a decision point and ask "what if you'd taken the other path?"

## Dashboard UI (localhost:3000/reasoning)

- Left sidebar: scrollable timeline of reasoning steps, each timestamped.
- Center: animated tree visualization (D3.js or Mermaid) showing decision branches in real time.
- Right sidebar: cost estimate for the current branch, token count, LLM model/temperature.
- Nodes colored by confidence (green = high, yellow = medium, red = low).
- Branch labels show decision criteria (e.g., "research needed" vs. "proceed").
- Playback controls: pause, rewind, fast-forward through thinking steps.

## API

```
WS /ws/reasoning/{session_id}
  -> {type: "token", content: "...", logprob: -0.5, timestamp: ...}
  -> {type: "decision", from_node: "...", options: [...], chosen: "..."}
GET /api/reasoning/{session_id}
  -> full reasoning trace after completion
```

Full reference: [[18_API_Reference]].

## Acceptance (DoD)

- [ ] Decision/execution trace renders in real time from canonical events ([[25_Event_System]]).
- [ ] User can pause and inspect decision branches.
- [ ] Auditable items captured: goal, plan/decision summaries, tool calls, state transitions, approvals, recovery decisions.
- [ ] Token-level data stored when the provider exposes it (optional); UI never claims private reasoning.

## Non-goals

- Exposing private hidden chain-of-thought (prohibited by v3.1 §18).
- Real-time token-level inference-cost optimization (v2 feature). This phase: visualization only.
- Manipulating the reasoning tree (e.g., forcing a different branch) — v2 feature.


---

---
title: Feature — Multi-Model Orchestration
type: feature
project: Agent System
status: spec
updated: 2026-09-06
feature_no: 2
up: "[[00_Index]]"
---

# Feature 2 — Multi-Model Orchestration

> **Source:** Spec v3.0 §4 · **Size:** Medium (Phase 11 in v3.1 plan — [[19_Execution_Plan]])
>
> [!warning] v3.1 Refinements (§19)
> - Use **provider adapters**; never hardcode fictional or unavailable provider model IDs (v3.0's `claude-opus-5` etc. are placeholders to be replaced by configured reality).
> - Model configuration must be **external/configurable**.
> - Implement: `ModelRegistry` · `ProviderRegistry` · `ModelSelector` · `PricingRegistry` · `CostPredictor`.
> - Selection factors: task type · latency · capability · context length · cost · availability · historical reliability.
> - **Every model invocation generates a ModelCall record** ([[24_Canonical_Domain_Model]]); cost accounting provider-aware per [[28_Reliability_Operations]] — unknown usage must not crash execution.

## What it does

Route each task/subtask to the most cost-effective or capable LLM. "Summarize logs" → cheap model. "Complex reasoning" → expensive model. "Code review" → medium. Configurable per agent type.

## Execution

```
core/orchestrator/llm_router_v2.py
├── ModelSelector    (per-task decision: cost vs. capability)
├── ProviderRegistry (Anthropic, OpenAI, free tier)
└── CostPredictor    (estimate tokens before committing)

config/model_selection_rules.json
{
  "code": {
    "primary": "claude-opus-5",
    "fallback": "gpt-4o",
    "budget_tier": "claude-sonnet-5"
  },
  "research": {
    "primary": "claude-sonnet-5",
    "budget_tier": "groq"
  },
  "summary": {
    "primary": "claude-haiku-4-5",
    "budget_tier": "groq"
  }
}
```

## Database

```sql
model_selection_log(
  id, task_id,
  candidate_models,   -- json list
  selected_model,
  decision_reason,
  cost_estimate_usd, actual_cost_usd,
  latency_ms, timestamp
)
```

See [[04_Data_Model]].

## Dashboard UI (localhost:3000/cost)

- Cost Optimizer tab: show per-task model selection and estimated vs. actual cost.
- Budget settings panel: set daily/monthly budget caps, alert thresholds.
- Multi-model selector: per-agent-type toggle between "fastest," "cheapest," "most-capable."

## API

```
GET  /api/models
  -> {available: [...], current_selection: {...}}
PUT  /api/models/{agent_type}/prefer
  -> {mode: "fastest|cheapest|capable", apply_to_all: false}
GET  /api/cost/summary?period=day|week|month
  -> {total_usd, by_model, by_agent_type, by_task}
```

Full reference: [[18_API_Reference]].

## Acceptance (DoD)

- [ ] Cost tracking table shows correct model selection per task.
- [ ] Recommendations engine proposes switches.
- [ ] Model rules hot-reloadable from `config/model_selection_rules.json`.
- [ ] Every invocation creates a `model_calls` row ([[04_Data_Model]]); unknown-cost calls complete without error and are flagged estimated.
- [ ] No hardcoded fictional model IDs — registry driven by configuration.

## Non-goals

- Live token counting across different quantized model variants (v2).


---

---
title: Feature — Autonomous Error Recovery
type: feature
project: Agent System
status: spec
updated: 2026-09-06
feature_no: 3
up: "[[00_Index]]"
---

# Feature 3 — Autonomous Error Recovery

> **Source:** Spec v3.0 §4 · **Size:** Medium (Phase 12 in v3.1 plan — [[19_Execution_Plan]])
>
> [!warning] v3.1 Refinements (§21)
> Error taxonomy: `network · timeout · rate_limit · validation · permission · dependency · syntax · test_failure · resource_limit · provider_failure · unknown`.
>
> **Never automatically retry:** destructive actions · permission failures · deterministic validation errors · repeated identical failures. Enforce max retries + exponential backoff + recovery history + repeated-failure detection. Recovery runs emit `recovery.started/completed/failed` events ([[25_Event_System]]).

## What it does

When a task fails, the agent doesn't just report failure — it introspects the error, proposes a fix, attempts recovery, and logs the pattern for future prevention.

## Execution

```
core/orchestrator/error_recovery.py
├── ErrorIntrospector (classify: network, timeout, LLM, logic, permission, resource)
├── RecoveryPlanner   (generate fix proposals)
├── RecoveryExecutor  (retry with adjusted params)
└── PatternLearner    (log failure patterns, suggest preventive actions)
```

## Database

```sql
failure_patterns(
  id, error_type, agent_type,
  root_cause_hypothesis_json,
  recovery_attempts,
  success_rate,
  preventive_action_json,
  last_seen
)
recovery_log(
  id, task_id,
  error_classification,
  original_error_msg,
  fix_proposed, fix_attempted, success,
  latency_impact_ms, timestamp
)
```

See [[04_Data_Model]].

## Flow

1. Task fails with error E.
2. ErrorIntrospector classifies E (e.g., "timeout in browser session").
3. RecoveryPlanner queries `failure_patterns` for similar cases.
4. Proposes fix: "retry with longer timeout," "reconnect browser," "escalate to user."
5. If user-approvable, attempts recovery (approval gate via Permission Layer).
6. Logs outcome to `recovery_log` and updates `failure_patterns`.

## Dashboard UI (localhost:3000/errors)

- Error Log tab: list of failures with classification, recovery status, and outcome.
- Patterns panel: show recurrent error types, preventive recommendations.
- One-click "apply prevention" button: auto-adjust agent params to avoid known failure modes.

## API

```
GET  /api/errors?agent_type=code&limit=50
  -> [{error_type, recovery_attempted, success, timestamp, task_id}]
GET  /api/errors/patterns
  -> [{error_type, frequency, recovery_success_rate, preventive_action}]
POST /api/errors/{id}/retry
  -> re-attempt the failed task with suggested fix
```

Full reference: [[18_API_Reference]].

## Acceptance (DoD)

- [ ] A task fails, agent auto-proposes a fix, recovery succeeds, pattern is logged.
- [ ] Non-retryable classes (destructive, permission, deterministic validation) never auto-retried.
- [ ] Max retries + backoff enforced; repeated identical failures detected and escalated.

## Non-goals

- ML-based root-cause analysis (v2).


---

---
title: Feature — Workspace Templates & Cloning
type: feature
project: Agent System
status: spec
updated: 2026-09-06
feature_no: 4
up: "[[00_Index]]"
---

# Feature 4 — Workspace Templates & Cloning

> **Source:** Spec v3.0 §4 · **Size:** Small (Phase 14 in v3.1 plan — [[19_Execution_Plan]])
>
> [!warning] v3.1 Refinements (§14, §22)
> Templates must **never** contain: `.env` · credentials · API keys · private keys · browser sessions · secret configuration.
> Allowed: source · dependency lockfiles · configuration templates · Git metadata (only where explicitly enabled) · documentation.
> Record **template version and source metadata**. Template creation goes through the permission gate ([[27_Security_Permissions]]) with secret scanning before snapshot.

## What it does

Snapshot a successful coding workspace state (files, env, git history) into a template. Reuse by cloning for similar future tasks. Enables rapid iteration and reduces setup time.

## Execution

```
core/sandbox/workspace_templates.py
├── TemplateSnapshotter (tar + metadata)
├── TemplateRegistry    (store, version, catalog)
└── TemplateCloner      (restore from template)
```

## Database

```sql
workspace_templates(
  id, name, description,
  source_workspace_id,
  snapshot_path,        -- tar.gz in templates/
  git_history_json,     -- commits from source
  tags_json,            -- ["python", "fastapi", "postgres"]
  created_at, used_count
)

-- workspace_sessions (extended)
template_id,            -- nullable, which template this came from
cloned_from_template    -- boolean
```

See [[04_Data_Model]].

## Flow

1. User completes a successful project (e.g., FastAPI + SQLite scaffold).
2. Clicks "Save as Template" in Workspace tab.
3. System snapshots: all files, `.env`, git history, Python dependencies (`requirements.txt`).
4. Stores in `templates/<template_id>/` with metadata in `workspace_templates`.
5. Next time: user starts new workspace, selects template from dropdown.
6. System clones template, restores files, re-initializes git, venv.

## Dashboard UI (localhost:3000/workspace)

- Templates sidebar: list of saved templates with tags and use count.
- "Save as Template" button on active workspace.
- "Clone from Template" button to create new workspace.
- Template detail view: shows source project, tags, size, git history, quick stats.

## API

```
POST   /api/workspace/{id}/save-template
  -> {name, description, tags}
GET    /api/templates
  -> list templates
POST   /api/workspace/clone-from-template/{template_id}
  -> creates new workspace from template
DELETE /api/templates/{id}
  -> remove template
```

Full reference: [[18_API_Reference]].

## Acceptance (DoD)

- [ ] Save workspace as template.
- [ ] Create new workspace from template — files restored exactly.
- [ ] Secret exclusion verified: `.env`, keys, credentials never present in snapshots.
- [ ] Template version + source metadata recorded.

## Non-goals

- Template versioning/branching (v2).


---

---
title: Feature — Agent Behavior Recording & Replay
type: feature
project: Agent System
status: spec
updated: 2026-09-06
feature_no: 5
up: "[[00_Index]]"
---

# Feature 5 — Agent Behavior Recording & Replay

> **Source:** Spec v3.0 §4 · **Size:** Medium (Phase 15 in v3.1 plan — [[19_Execution_Plan]])
>
> [!warning] v3.1 Refinements (§23)
> Replay modes: **INSPECT · SIMULATE · APPROVED_REEXECUTE**. Never blindly re-execute arbitrary historical actions.
> Before re-execution compare: workspace fingerprint · OS · dependencies · agent version · model configuration · recipe version · permissions.
> Destructive/network/payment actions require explicit approval. Recordings contain redacted data only ([[27_Security_Permissions]]).

## What it does

Record every action an agent takes (API calls, file changes, LLM prompts, decisions). Replay the sequence for debugging or auditing. Essential for understanding why an agent did something unexpected.

## Execution

```
core/audit/behavior_recorder.py
├── ActionCapture    (intercept all agent I/O)
├── ActionSerializer (convert to replay format)
└── ReplayEngine     (re-execute sequence)
```

## Database

```sql
behavior_recordings(
  id, session_id, agent_id,
  recording_start, recording_end,
  action_count,
  action_log_path      -- jsonl file in recordings/
)
```

Action log (`.jsonl`, appended):

```json
{
  "seq": 1,
  "timestamp": "2026-09-06T12:34:56Z",
  "action_type": "llm_call|file_write|shell_exec|decision",
  "details": {},
  "state_before": {},
  "state_after": {},
  "duration_ms": 123
}
```

See [[04_Data_Model]].

## Flow

1. Every subagent wraps its I/O with `RecordingContext`.
2. All actions (LLM calls, file ops, shell commands) are serialized to `.jsonl`.
3. User visits dashboard's "Recording Replay" tab, selects a session.
4. Replay engine step-through: execute each action, show state transition, pause on error or user click.
5. Can skip to a specific step or re-run from a checkpoint.

## Dashboard UI (localhost:3000/replay)

- Session selector: list recent recordings with duration and action count.
- Timeline scrubber: jump to any point in the recording.
- Step-through controls: play, pause, next, prev, 1x/2x/4x speed.
- Side panels:
  - Left: action log with clickable entries.
  - Center: live output/result of current step.
  - Right: agent state (variables, context, memory at current step).
- Diff viewer for file-write actions.

## API

```
GET  /api/recordings/{session_id}
  -> metadata + action log
GET  /api/recordings/{session_id}/replay/step/{seq}
  -> state at that step
POST /api/recordings/{session_id}/replay/resume-from/{seq}
  -> re-execute from checkpoint
```

Full reference: [[18_API_Reference]].

## Acceptance (DoD)

- [ ] A session is recorded, replay engine steps through actions.
- [ ] State transitions are visible step by step.
- [ ] All three replay modes boundary-tested ([[29_Testing_Strategy]] §Replay); re-execution blocked on fingerprint mismatch without approval.

## Non-goals

- Deterministic replay with exact environment restoration (v2).


---

---
title: Feature — Real-Time Cost Optimizer
type: feature
project: Agent System
status: spec
updated: 2026-09-06
feature_no: 6
up: "[[00_Index]]"
---

# Feature 6 — Real-Time Cost Optimizer

> **Source:** Spec v3.0 §4 · **Size:** Medium
>
> [!warning] v3.1 Refinements (§20)
> - Cost accounting is **provider-aware** — never assume all providers expose identical token metrics. Support input/output/cached tokens, estimated tokens, and unknown usage.
> - **Unknown cost must not crash execution** — mark estimated/unknown values explicitly.
> - Budget levels: 50% / 75% / 90% / 100%. Budget scopes: **per-task · per-session · daily · per-provider**.
> - `cost.recorded` is a canonical event ([[25_Event_System]]); ModelCall records per [[24_Canonical_Domain_Model]].

## What it does

Live cost tracking, per-agent and per-task. Alerts when approaching budget. Recommends model switches or task deferral to stay within budget.

## Execution

```
core/features/cost_optimizer.py
├── CostTracker          (log every LLM call, calculate cost)
├── BudgetMonitor        (check remaining budget)
├── CostAlertManager     (email/push/dashboard alerts)
└── RecommendationEngine (suggest model downgrade or task deferral)
```

## Database

```sql
cost_budget(
  id,
  period,               -- daily|weekly|monthly
  limit_usd,
  current_period_start,
  alert_threshold_pct   -- alert at 80%
)

-- llm_usage_log (extended):
model_selected, cost_usd, latency_ms, tokens_in, tokens_out
```

See [[04_Data_Model]].

## Flow

1. Every LLM call logs tokens + model to `llm_usage_log`.
2. CostTracker calculates cost in real time.
3. BudgetMonitor checks against `cost_budget` for current period.
4. At thresholds (50%, 75%, 90%), trigger alerts.
5. RecommendationEngine suggests: "Use Claude Haiku for summaries instead of Opus" or "Defer non-urgent tasks until tomorrow."
6. User can approve suggestions; optimizer applies model selection changes.

## Dashboard UI (localhost:3000/cost)

- Top banner: current period cost, budget remaining, % used, projected end-of-period cost.
- Donut chart: cost breakdown by agent type.
- Table: recent LLM calls with model, tokens, cost, latency.
- Recommendations section: clickable suggestions (e.g., "Save $5.42/day by using Haiku for summarization").
- Budget settings: set period, limit, alert thresholds.

## API

```
GET  /api/cost/status
  -> {period, limit, spent, remaining, pct_used, trend}
GET  /api/cost/breakdown?group_by=agent|model|task
  -> cost by dimension
GET  /api/cost/recommendations
  -> [{action, savings_usd, impact_description}]
POST /api/cost/budget
  -> {period, limit_usd, alert_threshold_pct}
```

Full reference: [[18_API_Reference]].

## Acceptance (DoD)

- [ ] Budget alerts fire at 50/75/90/100% thresholds.
- [ ] Per-call cost logged with model + tokens (provider-aware; cached tokens captured where available).
- [ ] Unknown/estimated usage explicitly flagged; execution never crashes on missing cost data.
- [ ] Per-task, per-session, daily, and per-provider budgets enforced.
- [ ] Recommendations generated and clickable.

## Non-goals

- Predictive budget overflow (v2).


---

---
title: Feature — Intelligent Task Batching
type: feature
project: Agent System
status: spec
updated: 2026-09-06
feature_no: 7
up: "[[00_Index]]"
---

# Feature 7 — Intelligent Task Batching

> **Source:** Spec v3.0 §4 · **Size:** Medium
>
> [!warning] v3.1 Refinements (§24)
> Only batch **compatible** tasks. Compatibility considers: agent type · workspace · task type · required permissions · dependencies · resource requirements. Never mix unrelated secrets or contexts.
> Implement: `batch_id` · idempotency · cancellation · partial failure · concurrency limits · per-task result tracking.

## What it does

When multiple similar tasks are queued, the orchestrator automatically groups and parallelizes them, reducing overhead and improving throughput.

## Execution

```
core/orchestrator/task_batcher.py
├── TaskSimilarityScorer (embedding-based grouping)
├── BatchOptimizer       (decide parallelism strategy)
└── BatchExecutor        (run tasks concurrently in worker pool)
```

## Logic

- When ≥3 tasks of same type queued (e.g., "research X," "research Y," "research Z"):
  - Score similarity via embedding distance.
  - If score > threshold, group into a batch.
  - Create a meta-task: `batch_research([...])`.
  - ResearchAgent processes all at once: search for all, fetch all, summarize all, deduplicate insights.
  - Return results as individual task outcomes.

## Database

```sql
task_batches(
  id,
  batch_type,        -- "code_review", "research", etc.
  task_ids_json,     -- list of original task IDs
  member_count,
  created_at, completed_at,
  speedup_factor     -- ratio of sequential vs. batch time
)
```

See [[04_Data_Model]].

## Benefit

If 3 research tasks take 30s each sequentially (90s), batching them into one call with parallel search/fetch reduces to ~35s. **2.5x speedup.**

## Dashboard UI (localhost:3000/kanban)

- When tasks are batched, show them grouped with a "Batch" badge.
- Hover over batch: show member tasks, estimated speedup.
- Toggle "Enable Auto-Batching" in settings.

## API

```
GET /api/tasks/batching-config
  -> {enabled, similarity_threshold, min_batch_size}
PUT /api/tasks/batching-config
  -> update settings
GET /api/tasks/batches?limit=20
  -> recent batches with member counts and speedup metrics
```

Full reference: [[18_API_Reference]].

## Acceptance (DoD)

- [ ] ≥3 similar queued tasks auto-group into a batch.
- [ ] Speedup factor recorded per batch.
- [ ] Batch idempotency, cancellation, and partial-failure handling verified.
- [ ] Incompatible tasks never mixed (permissions/workspace/secret contexts).

## Non-goals

- Cross-agent batching (e.g., code review + research in one batch). Single-type batching only, v1.


---

---
title: Feature — Autonomous QA & Testing
type: feature
project: Agent System
status: spec
updated: 2026-09-06
feature_no: 8
up: "[[00_Index]]"
---

# Feature 8 — Autonomous QA & Testing

> **Source:** Spec v3.0 §4 · **Size:** Medium (Phase 13 in v3.1 plan — [[19_Execution_Plan]])
>
> [!warning] v3.1 Refinements (§25)
> **Generated tests are untrusted code.** Run inside the sandbox with: no host filesystem · no host credentials · restricted network · CPU limit · memory limit · timeout.
> Reports must be structured: tests generated · executed · passed · failed · skipped · duration · coverage (when available) · failure diagnostics.

## What it does

When a CodeAgent finishes writing code, it auto-generates unit tests, runs them, and reports coverage. Catches regressions early.

## Execution

```
core/agents/qa_agent.py
├── TestGenerator    (LLM: "write tests for this code")
├── TestRunner       (pytest/mocha/etc. runner, language-aware)
├── CoverageAnalyzer (coverage.py, nyc, etc.)
└── ReportBuilder    (HTML report, metrics table)
```

## Database

```sql
qa_reports(
  id, task_id, code_file,
  tests_generated, tests_passed, tests_failed,
  coverage_pct,
  coverage_report_html,
  timestamp
)
```

See [[04_Data_Model]].

## Flow

1. CodeAgent commits a change to `workspace/src/main.py`.
2. QAAgent introspects: "this is Python, pytest is available."
3. Generates test suite based on the code and commit message.
4. Runs `pytest` inside the workspace container.
5. Captures pass/fail, coverage stats.
6. Logs report to `qa_reports`, stores HTML in `outputs/`.
7. If coverage < 80%, flags as "low coverage" in the task result.

## Dashboard UI (localhost:3000/workspace)

- QA tab: show QA reports for recent code commits.
- Test results table: test name, status, execution time.
- Coverage badges: by file, by function.
- "View Full Report" link: opens HTML coverage report.

## API

```
GET /api/qa/reports?task_id=...
  -> [{tests_generated, passed, failed, coverage_pct}]
GET /api/qa/reports/{id}/html
  -> HTML coverage report
```

Full reference: [[18_API_Reference]].

## Acceptance (DoD)

- [ ] Tests auto-generated and run after CodeAgent commits.
- [ ] Coverage stored and flagged when < 80%.
- [ ] Sandbox restrictions verified: no host FS/credentials, restricted network, CPU/memory caps, timeout ([[27_Security_Permissions]]).
- [ ] Structured report persisted (generated/executed/passed/failed/skipped/duration/coverage/diagnostics).

## Non-goals

- Integration test generation (v2). Unit tests only, v1.


---

---
title: Feature — Workflow Recipe Library
type: feature
project: Agent System
status: spec
updated: 2026-09-06
feature_no: 9
up: "[[00_Index]]"
---

# Feature 9 — Workflow Recipe Library

> **Source:** Spec v3.0 §4 · **Size:** Medium (Phase 16 in v3.1 plan — [[19_Execution_Plan]])
>
> [!warning] v3.1 Refinements (§26)
> Recipes must be **versioned**. Execution uses the canonical Task/Event system ([[24_Canonical_Domain_Model]], [[25_Event_System]]) — never a separate path. Support: DAG dependencies · parameters · validation · execution history · cancellation · failure handling.
> **Recipes must not bypass permission checks.**

## What it does

Compose and save multi-step workflows (sequences of agent calls with dependencies). Reuse with one click. "Automated Blog Publishing Workflow" = research → write → generate image → create PDF → upload. Save, then trigger anytime.

## Execution

```
core/recipes/recipe_manager.py
├── RecipeBuilder   (compose from task DAG)
├── RecipeRegistry  (store, version)
└── RecipeExecutor  (instantiate and run)
```

## Database

```sql
recipes(
  id, name, description,
  task_dag_json,      -- task graph definition
  parameters_json,    -- {param_name: default_value}
  tags_json,
  created_at, executions_count
)
```

See [[04_Data_Model]].

## Recipe JSON structure

```json
{
  "name": "Blog Publishing Pipeline",
  "params": {
    "topic": "string",
    "output_format": ["pdf", "pptx"]
  },
  "tasks": [
    {"id": "t1", "type": "research", "input": {"query": "${topic}"}},
    {"id": "t2", "type": "document", "depends_on": ["t1"], "input": {"outline": "${t1.result}"}},
    {"id": "t3", "type": "browser", "depends_on": ["t2"], "input": {"upload": "${t2.result}"}}
  ]
}
```

## Flow

1. User completes a multi-step workflow manually once (research + write + format).
2. Clicks "Save as Recipe" from the task graph view.
3. System extracts the DAG, prompts for recipe name + params.
4. Stores to `recipes` table.
5. Next time: user goes to Recipes tab, clicks recipe, fills in params, hits "Run Recipe."
6. System instantiates the DAG, substitutes params, submits to Supervisor.
7. Workflow executes exactly like before, but in one click.

## Dashboard UI (localhost:3000/recipes)

- Recipes tab: gallery of saved recipes with descriptions, tags, usage count.
- Recipe detail: show DAG visualization, parameter form, quick-run button.
- "Save as Recipe" button visible in Kanban view after a multi-task workflow completes.
- Recipe execution history: show past runs with parameters and outcomes.

## API

```
GET  /api/recipes
  -> [{name, description, param_count, usage_count}]
POST /api/recipes
  -> {name, description, task_dag, parameters}
POST /api/recipes/{id}/execute
  -> {param_values} -> submits to supervisor, returns session_id
GET  /api/recipes/{id}/executions
  -> past runs
```

Full reference: [[18_API_Reference]].

## Acceptance (DoD)

- [ ] Multi-task workflow saved as recipe from Kanban view.
- [ ] One-click re-run with parameter substitution.
- [ ] Recipes versioned; execution through canonical task/event pipeline.
- [ ] Permission checks enforced inside recipe execution; cancellation and partial failure handled.

## Non-goals

- Recipe marketplace (v3). Local library only, v1.


---

---
title: Feature — Agent Personality & Tone Adaptation
type: feature
project: Agent System
status: spec
updated: 2026-09-06
feature_no: 10
up: "[[00_Index]]"
---

# Feature 10 — Agent Personality & Tone Adaptation

> **Source:** Spec v3.0 §4 · **Size:** Medium (Phase 17 in v3.1 plan — [[19_Execution_Plan]])
>
> [!warning] v3.1 Refinements (§27)
> Personality is a **presentation/behavior layer**. It must NOT silently modify: security policies · permission policies · system safety constraints · resource limits.
> Configuration must be: **versioned · user-visible · persisted · auditable**. Feedback must not silently rewrite core system instructions.

## What it does

Customize how agents communicate (verbose vs. terse, formal vs. casual) and their reasoning style (fast heuristic vs. careful step-by-step). Learned per-agent preferences over time.

## Execution

```
core/agents/personality.py
├── PersonalityConfig        (store per-agent: tone, verbosity, reasoning_style)
├── PersonalityInference     (LLM system prompt injection)
└── FeedbackCollector        (learn from user ratings)
```

## Database

```sql
agent_personalities(
  id, agent_id,
  tone,                     -- "formal", "casual", "terse", "verbose"
  verbosity,                -- 1–10 scale
  reasoning_style,          -- "fast", "careful", "socratic"
  system_prompt_override,
  learned_from_feedback_count,
  updated_at
)

feedback_log(
  id, agent_id, session_id,
  rating,                   -- 1–5 stars
  comment,
  timestamp
)
```

See [[04_Data_Model]].

## Flow

1. User rates an agent's output: "I liked the concise answer, give more of that" (5 stars + comment).
2. FeedbackCollector stores to `feedback_log`.
3. After N feedback items (e.g., 10), PersonalityInference re-analyzes trend: "user prefers terse output."
4. Adjusts `agent_personalities.verbosity` or `tone`.
5. On next agent call, inject adjusted system prompt: "Answer concisely in 1–2 sentences."

## Dashboard UI (localhost:3000/agents)

- Agent settings panel: tone selector (formal/casual/terse/verbose), verbosity slider (1–10), reasoning_style buttons.
- Feedback box after agent output: quick 1–5 star rating + optional comment.
- "Learned preferences" section: show inferred tone/verbosity based on feedback, with confidence score.
- "Reset to Default" button to revert customizations.

## API

```
GET  /api/agents/{id}/personality
  -> {tone, verbosity, reasoning_style, system_prompt_override}
PUT  /api/agents/{id}/personality
  -> update personality config
POST /api/agents/{id}/feedback
  -> {rating, comment}
```

Full reference: [[18_API_Reference]].

## Acceptance (DoD)

- [ ] Feedback collected via star rating.
- [ ] After N ratings, personality config adjusts automatically.
- [ ] System prompt override injected on next agent call.
- [ ] Personality config versioned and auditable; cannot alter security/permission/safety/resource settings.
- [ ] Core system instructions never silently rewritten by feedback.

## Non-goals

- Multi-user personality profiles (v2). Single user, v1.


---

---
title: Feature — Autonomous Insight Generation
type: feature
project: Agent System
status: spec
updated: 2026-09-06
feature_no: 12
up: "[[00_Index]]"
---

# Feature 12 — Autonomous Insight Generation

> **Source:** Spec v3.0 §4 · **Size:** Medium (Phase 17 in v3.1 plan — [[19_Execution_Plan]])
>
> [!warning] v3.1 Refinements (§28)
> Insights must be derived **only from canonical persisted events** ([[25_Event_System]]). Potential insights: recurring failures · expensive workflows · slow agents · model reliability · common task types · productivity trends · repeated recovery patterns.
> Insights must respect **secret redaction** ([[27_Security_Permissions]]). **Never generate fabricated analytics.**

## What it does

Scheduled jobs that periodically analyze logs, results, and memories to generate summaries, reports, trend analysis, and anomaly alerts. User wakes up to a "Daily Briefing" or "Weekly Insights" generated by the agent.

## Execution

```
core/features/insight_generator.py
├── InsightScheduler (daily/weekly jobs)
├── DataAggregator   (pull logs, memories, results)
├── AnalysisEngine   (LLM: analyze trends, spot anomalies)
└── ReportGenerator  (format as HTML email or dashboard card)
```

## Database

```sql
insights(
  id,
  insight_type,       -- "daily_briefing", "weekly_summary", "anomaly", "trend"
  generated_at,
  content_html,
  key_findings_json,
  archived_at
)
```

See [[04_Data_Model]].

## Flow

1. Scheduler fires daily at 8 AM: "Generate Daily Briefing."
2. DataAggregator queries last 24h: tasks completed, errors, cost spent, time saved estimates.
3. AnalysisEngine runs LLM prompt: "Summarize the agent's activity: what was accomplished, what failed, what patterns emerged?"
4. ReportGenerator formats as HTML email.
5. User receives email with: tasks completed, cost summary, errors recovered, recommendations for tomorrow.

## Dashboard UI (localhost:3000/insights)

- Insights tab: list of generated reports.
- Daily Briefing card: quick stats (tasks completed, errors, cost, time saved).
- Weekly Summary card: trend analysis, most-used agent types, common errors, cost trends.
- Anomalies section: "Unusual: CodeAgent success rate dropped from 95% to 75%" with suggested actions.
- Archive/delete buttons to clean up old insights.

## API

```
GET  /api/insights?type=daily_briefing&limit=7
  -> recent insights
GET  /api/insights/{id}
  -> full HTML content
POST /api/insights/config
  -> {enabled, frequency: "daily|weekly", delivery: "email|dashboard"}
```

Full reference: [[18_API_Reference]].

## Acceptance (DoD)

- [ ] Daily briefing generated on schedule and visible in dashboard.
- [ ] Anomalies surfaced with suggested actions.
- [ ] All insights traceable to source events; no fabricated metrics.
- [ ] Secret redaction verified in generated content.

## Non-goals

- Real-time anomaly detection (v2). Batch analysis only, v1.


---

---
title: Web Dashboard UI/UX
type: spec
project: Agent System
status: locked
updated: 2026-09-06
up: "[[00_Index]]"
---

# 5. Localhost Web Dashboard — UI/UX Specification

> **Source:** Spec v3.0 §5 · Next.js 15 + Tailwind + shadcn/ui · `localhost:3000`
>
> [!warning] v3.1 Frontend Rule (§33)
> The frontend must consume **real backend contracts** (`/api/v1`, [[18_API_Reference]]). NEVER create production UI that merely pretends functionality exists — no fake costs, agents, reasoning, approvals, task status, replay, analytics, downloads, or progress. Fixtures are allowed only in tests/development. Every production UI control must have a real backend implementation.
> All realtime views (reasoning trace, task progress, chat) must implement reconnect + resume-from-sequence per [[25_Event_System]].

## 5.1 Design System

### Color Palette

```css
--primary: #3b82f6       /* Bold blue, CTA buttons */
--primary-dark: #1e40af  /* Darker blue, hover */
--secondary: #8b5cf6     /* Purple, agent status */
--success: #10b981       /* Green, task complete */
--warning: #f59e0b       /* Orange, needs attention */
--danger: #ef4444        /* Red, errors */
--neutral-50: #f9fafb
--neutral-100: #f3f4f6
--neutral-900: #111827
--bg-dark: #0f172a       /* Deep navy for dark mode */
--text-primary: #1f2937
--text-secondary: #6b7280
--border: #e5e7eb
```

### Typography

```
Font stack: Inter, system-ui, sans-serif
Display (H1): 32px, weight 700, line-height 1.2
Heading (H2): 24px, weight 600, line-height 1.3
Title (H3): 20px, weight 600, line-height 1.4
Body: 14px, weight 400, line-height 1.6
Small: 12px, weight 400, line-height 1.5
Code: Fira Code, 12px, weight 400, monospace
```

### Spacing

Tailwind default 4px grid: 4, 8, 12, 16, 24, 32, 48, 64, 80, 96.

### Shadows & Depth

```
Shadow-sm: 0 1px 2px 0 rgba(0, 0, 0, 0.05)
Shadow-md: 0 4px 6px -1px rgba(0, 0, 0, 0.1)
Shadow-lg: 0 10px 15px -3px rgba(0, 0, 0, 0.1)
Shadow-xl: 0 20px 25px -5px rgba(0, 0, 0, 0.1)
Glass effect: backdrop-blur-md, bg-white/80, border border-white/20
```

## 5.2 Main Layout

```
┌─────────────────────────────────────────────────────────────┐
│  Logo | Breadcrumb              [Search] [Notifications] [⚙] │  Header (sticky)
├──────────┬────────────────────────────────────────────────────┤
│ Sidebar  │  MAIN CONTENT AREA (responsive)                  │
│ (collap) │                                                    │
│ ↓        │  ↓ Below: 11 Main Tabs                            │
└──────────┴────────────────────────────────────────────────────┘
```

### Sidebar (left, collapsible)

- Logo + system name ("Agent System") at top.
- Main navigation:
  - 🏠 Dashboard
  - 💬 Chat
  - 📊 Kanban
  - 🖥️ Workspace
  - 📚 Vault
  - 📁 Outputs
  - 📅 Schedule
  - ✅ Approvals (with red badge count if pending)
  - 📋 Templates
  - 💰 Cost
  - 📖 Recipes
  - 🔍 Reasoning
  - 📊 Insights
  - 🔧 Settings
  - 📜 Audit Log
- Footer: connection status, service uptime, quick stats (active agents, queue depth).

### Sidebar styling

- Dark background (`--bg-dark` or `--neutral-900`).
- Light text.
- Hover: subtle background shift.
- Active tab: left border accent (`--primary`), bold text.
- Collapse button: hamburger icon, smooth animation.

### Header (top)

- Left: Logo + breadcrumb trail (e.g., "Agent System > Chat > Session #5").
- Center: search input (full-width, autocomplete: agents, tasks, vault notes).
- Right:
  - Bell icon (notifications, unread count badge).
  - Settings icon (quick toggles: dark mode, notifications on/off).
  - User avatar (for multi-device future, greyed out now).

## 5.3 Chat Tab (localhost:3000/chat)

```
┌────────────────────────────────────┐
│  Chat History / Sessions Selector  │  (collapsible sidebar)
│  [New Chat] [Session #5] [Session] │
├────────────────────────────────────┤
│  Chat Messages Area (scrollable)    │
│                                    │
│  ┌───────────────┐                │
│  │ User: "Build  │  (message bubble)
│  │ a fast API"   │  Message styling:
│  └───────────────┘  - User: blue, right-aligned
│                     - Agent: grey, left-aligned
│  ┌─────────────────────────────────┐
│  │ Agent: "I'll set up...          │
│  │ [START] Framework scaffolding   │
│  │         ✓ Complete              │
│  │ [FIX]   Database migration      │
│  │         ⏳ Running               │
│  └─────────────────────────────────┘
│                                    │
├────────────────────────────────────┤
│ Input: [Type your message here...] │
│        [Attach file] [Emoji] [Send]│
└────────────────────────────────────┘
```

### Agent message enhancement

- Inline badges for task status: [✓ Complete], [⏳ Running], [✗ Failed].
- Expandable task details: click badge to see full task output/logs.
- Code snippets: inline syntax highlighting (Prism.js), copy button.
- Links to other tabs: "View in Workspace" button on code-related messages.

### Right-side panel (when selected)

- Active agents list: agent name, type, status (green/yellow/red), resource usage (CPU%, memory%).
- Kill button per agent.

### Animations

- New message slides in from bottom with fade.
- Typing indicator (three bouncing dots) when agent is generating.
- Token count update live in top-right (e.g., "1,250 tokens used").

## 5.4 Kanban Tab (localhost:3000/kanban)

```
┌──────────────────────────────────────────────────────────┐
│ [Goal Input] ▶ Submit Goal                               │
├──────────────────────────────────────────────────────────┤
│                                                           │
│  Pending      In Progress   Review        Done    Failed │
│  ┌──────┐     ┌──────┐     ┌──────┐      ┌────┐  ┌────┐ │
│  │ t1   │     │ t2   │     │ t3   │      │ t4 │  │ t5 │ │
│  │ Desc │ --> │(🟡)  │ --> │(👁️) │  --> │ ✓  │  │ ✗  │ │
│  └──────┘     └──────┘     └──────┘      └────┘  └────┘ │
│                                                           │
│  ┌──────┐                                                 │
│  │ t6   │                                                 │
│  │ ...  │                                                 │
│  └──────┘                                                 │
└──────────────────────────────────────────────────────────┘
```

### Card styling

- Each task = card with title, agent type (badge), status icon, progress bar (if applicable).
- Colors:
  - Pending: `--neutral-200`
  - In Progress: `--primary` (blue)
  - Review: `--warning` (orange)
  - Done: `--success` (green)
  - Failed: `--danger` (red)
- Click card to expand: show full task details, logs, dependencies, estimated time.
- Drag-and-drop within columns (for user override, low-priority).
- Batched tasks: grouped card with "Batch" badge, expandable to show members ([[11_Feature_Task_Batching]]).

### Dependency visualization

- Arrows between cards showing task dependencies.
- Hover arrow to highlight path.

### Top toolbar

- View selector: [Timeline] [Kanban] [Tree/DAG] buttons.
- Filter: agent type, status, tag, date.
- Sort: by priority, by agent, by time, by cost.

## 5.5 Workspace Tab (localhost:3000/workspace)

```
┌─────────────────────────────────────────────────────────┐
│ [New Workspace] [Open...] [Workspace selector dropdown] │
├──────────────────┬──────────────────────────────────────┤
│ File Tree        │ Editor / Diff Viewer / Terminal        │
│ (left, narrow)   │                                        │
│                  │ [Tabs: Editor | Diff | Terminal | QA] │
│ 📁 src/          │                                        │
│  ├ main.py       │ Content area (syntax-highlighted)      │
│  ├ utils.py      │                                        │
│ 📁 tests/        │                                        │
│ 📄 .gitignore    │                                        │
│ 📄 requirements  │                                        │
│                  │ [Bottom toolbar]                       │
│ [Template Snap]  │ [Run | Test | Commit | Diff | Log]    │
│ [Clone]          │                                        │
└──────────────────┴──────────────────────────────────────┘
```

### Left panel (file tree)

- Collapsible folders with icons.
- Right-click menu: new file, delete, rename, copy path.
- Syntax highlighting icon indicator (`.py` = Python icon, `.json` = JSON icon, etc.).
- Click file to open in editor.

### Center panel (editor/diff/terminal)

- **Editor tab:** syntax highlighting (Highlight.js), line numbers, code folding, dark/light themes.
- **Diff tab:** before/after view with line-by-line coloring (red=removed, green=added). Clickable "Approve this change" / "Reject" buttons.
- **Terminal tab:** live output stream from agent's shell commands. Black background, green text (optional retro theme). Copy-to-clipboard button per output block.
- **QA tab:** test results, coverage badges, HTML report embed ([[12_Feature_Autonomous_QA]]).

### Right panel (when active)

- Git log: recent commits with author (CodeAgent), timestamp, message.
- Workspace info: disk usage, last modified, sandbox container ID.
- "Save as Template" button ([[08_Feature_Workspace_Templates]]).
- "Clone from Template" dropdown.

### Bottom toolbar

- [Run Tests] button: triggers QA pipeline, shows live results.
- [Git Diff] button: shows all uncommitted changes.
- [Commit] button: prompts for message, commits (signed as CodeAgent).
- [Kill Sandbox] button: force-restart the workspace container.

### Animations

- File tree expands/collapses smoothly.
- Tab switching fades in.
- Diff highlighting animates (blink effect on changed lines).

## 5.6 Vault Tab (localhost:3000/vault)

```
┌────────────────────────────────────────────────────────┐
│ [Search input] [Create new note] [Obsidian sync status]│
├─────────────────────┬────────────────────────────────┤
│ Note List           │ Note Content                    │
│ (tree or flat)      │ (markdown preview/edit)         │
│                     │                                 │
│ 📌 Daily Notes      │ # Decision: Auth System        │
│ ├ 2026-09-06        │ **Date:** 2026-09-05            │
│ ├ 2026-09-05        │ **Tags:** [auth, security]      │
│ 📁 Decisions        │ **Related:**                     │
│ ├ Auth Reconciliat  │ [[User Model]] [[JWT Strategy]] │
│ ├ Memory Vault      │                                 │
│ 📁 Projects         │ We decided to use Better Auth   │
│ ├ GraftAI           │ because...                      │
│ ├ VibeCoder         │                                 │
│ 📁 Agents           │ ---                             │
│ └ Research          │ [Backlinks to this note]        │
│                     │ - [[Project Dashboard]]         │
│                     │ - [[LLM Decision]]              │
└─────────────────────┴────────────────────────────────┘
```

### Left panel (notes tree)

- Hierarchical folder structure mirroring Obsidian vault layout.
- Search input filters in real time (full-text on title + content).
- "Create new note" button opens a form: title, parent folder, tags, template.
- Sync status indicator: green checkmark if synced with local Obsidian, yellow if pending, red if conflict.

### Right panel (note content)

- Markdown renderer (Remark/Rehype) with syntax highlighting.
- Wiki-link rendering: `[[Note Name]]` → clickable link to that note.
- Backlinks section at bottom: which other notes link to this one.
- Edit toggle (pencil icon): switches to CodeMirror editor with markdown preview side-by-side.
- Save button (if editing).
- Delete button with confirmation.

### Tag cloud (bottom-left)

All tags used across vault, clickable to filter.

### Animations

- Note list updates live if agent writes to vault (re-sorts, highlights new note).
- Wiki-links highlight on hover.

## 5.7 Outputs Tab (localhost:3000/outputs)

```
┌──────────────────────────────────────────────────────┐
│ Generated Files / Downloads                          │
├──────────────────────────────────────────────────────┤
│ Filter: [All] [PPTX] [PDF] [DOCX] [XLSX]             │
│ Sort: [Newest] [Size] [Name]                         │
│                                                      │
│ ┌─────────────────────────────────────────────────┐  │
│ │ 📊 Q3 Financial Report                          │  │
│ │ .pptx | 2.4 MB | Generated 2h ago               │  │
│ │ Task: #34 (Document Agent)                      │  │
│ │ [Preview] [Download] [Delete] [Open in Obsidian]│  │
│ └─────────────────────────────────────────────────┘  │
│                                                      │
│ ┌─────────────────────────────────────────────────┐  │
│ │ 📄 Research Summary                             │  │
│ │ .pdf | 1.8 MB | Generated 4h ago                │  │
│ │ Task: #28 (Document Agent)                      │  │
│ │ [Preview] [Download] [Delete]                   │  │
│ └─────────────────────────────────────────────────┘  │
│                                                      │
│ ┌─────────────────────────────────────────────────┐  │
│ │ 📈 Budget Analysis                              │  │
│ │ .xlsx | 512 KB | Generated 1d ago               │  │
│ │ Task: #15 (Document Agent)                      │  │
│ │ [Preview] [Download] [Delete]                   │  │
│ └─────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────┘
```

### Card per output

- Icon (file type), filename, size, created time, task reference.
- Preview button: opens in modal (PPTX → thumbnail carousel, PDF → embedded viewer, XLSX → table, DOCX → formatted view).
- Download button: direct download link.
- Delete button with confirmation.

### Bulk actions

- Select multiple files, bulk download as ZIP.
- Bulk delete with confirmation.

## 5.8 Additional Tabs (Brief)

### Schedule Tab (localhost:3000/schedule)

- Cron job list: name, schedule (human-readable), next run, last run status.
- [New Job] button: form to create cron/interval/date/webhook jobs.
- Job detail: show full config, run history table, manual trigger button.

### Approvals Tab (localhost:3000/approvals)

- Pending actions queue: action description, scope, context (screenshot/diff preview if available).
- [Approve] [Deny] [Always Allow This Type] buttons per action.
- Color-coded by scope: red = high-risk (browser:transact, os:input), yellow = medium (file:write).

### Templates Tab (localhost:3000/templates)

- Gallery of workspace templates: thumbnail, name, description, tags, use count.
- [New Workspace from Template] selector.
- [Save Current as Template] button.

### Cost Tab (localhost:3000/cost)

- Cost summary: current period total, budget remaining, % used, trend arrow.
- Breakdown chart: by agent type, by LLM model, by task.
- Recent LLM calls table: model, tokens, cost, latency.
- Recommendations section: suggested model switches or task deferrals.
- Budget settings form: set period, limit, alert threshold.

### Recipes Tab (localhost:3000/recipes)

- Recipe gallery: name, description, DAG visualization thumbnail, tags, usage count.
- [Create New Recipe] button (from existing task graph or from scratch).
- Recipe detail: full DAG view, parameter form, execution history.
- [Run Recipe] button: fill params, submit.

### Reasoning Tab (localhost:3000/reasoning)

- Session selector: list recent agent reasoning sessions.
- Real-time reasoning tree visualization (D3.js).
- Timeline scrubber, step-through controls.
- Decision node inspection sidebar.

### Insights Tab (localhost:3000/insights)

- Insights gallery: daily briefing, weekly summary, anomalies, trends.
- Card per insight: key findings summary, full HTML view link.
- Archive/delete buttons.

### Audit Log Tab (localhost:3000/audit)

- Table: actor (agent/user), action, scope, approval source, outcome, timestamp.
- Filter: by actor, by scope, by date range, by outcome (approved/denied/auto).
- Export button: CSV/JSON download.

### Settings Tab (localhost:3000/settings)

- Appearance: dark/light mode toggle, accent color picker.
- Agent settings: per-agent personality (tone, verbosity), trust levels per scope.
- Notification settings: email/push/dashboard.
- Service settings: port, bind address, enable/disable features.

## Related

- CLI spec: [[17_CLI_Specification]]
- API endpoints: [[18_API_Reference]]


---

---
title: CLI Specification
type: spec
project: Agent System
status: locked
updated: 2026-09-06
up: "[[00_Index]]"
---

# 6. CLI Tool — TUI/Output Specification

> **Source:** Spec v3.0 §6 · Entry point: `agentctl`
>
> [!warning] v3.1 CLI Rule (§34)
> CLI commands must use the **same API/domain contracts** as the web dashboard (`/api/v1`, [[18_API_Reference]]) — no separate CLI-only business logic. Support `--json` (machine-readable, stable schema), `--verbose`, `--no-color`. Stable exit codes for scripting. The Reasoning command surfaces the **Decision & Execution Trace** ([[05_Feature_Reasoning_Trace_Viewer]]) — auditable events, not private reasoning.

## 6.1 Framework & Styling

**Framework:** Typer (v1, Python) → custom Rust binary (v3) wrapping the same HTTP API.

**Styling libraries:**

- Rich (Python): tables, panels, progress bars, syntax highlighting, colors, spinners.
- Prompt-toolkit (Python): multi-line input, autocomplete, key bindings.
- Clap (Rust v3): argument parsing, subcommands.

**Color scheme (matches web dashboard [[16_Dashboard_UIUX]]):**

```
Primary: Bright Blue (#0088FF)
Success: Bright Green (#00DD00)
Warning: Bright Orange (#DD8800)
Error:   Bright Red (#DD0000)
Muted:   Dim (#888888)
```

## 6.2 Main Commands

```bash
agentctl --help
```

```
╭──────────────────────────────────────────────────────────╮
│                   🤖 AGENT SYSTEM                        │
│                Local AI Agent Orchestrator                │
│                    v3.0 — Ready                          │
╰──────────────────────────────────────────────────────────╯

Usage: agentctl [OPTIONS] COMMAND [ARGS]

Commands:
  chat         💬 Submit a goal to the supervisor agent
  status       📊 Display system status and active agents
  agents       👥 Manage agents (list, kill, inspect)
  tasks        ✅ Manage tasks (list, inspect, retry, cancel)
  workspace    🖥️  Manage coding workspaces
  vault        📚 Manage memory vault notes
  documents    📄 Manage generated documents
  browser      🌐 Manage browser sessions
  autopilot    🎮 Manage autopilot / desktop control sessions
  schedule     📅 Manage scheduled jobs
  approvals    ✔️  Manage pending approvals
  recipes      📖 Manage workflow recipes
  cost         💰 Show cost tracking and budget
  insights     💡 Generate and view insights
  memory       🧠 Query agent memory
  logs         📜 Tail service logs
  start        🚀 Start the agent service
  stop         ⏹️  Stop the agent service
  restart      🔄 Restart the agent service
  config       ⚙️  Show/edit configuration
  version      ℹ️  Show version info

Options:
  -h, --help         Show this help message
  -v, --verbose      Verbose output
  --json             Output in JSON format (for scripting)
  --no-color         Disable colored output

Examples:
  agentctl chat "Build a FastAPI scaffold"
  agentctl status
  agentctl workspace list
  agentctl vault search "auth decision"
  agentctl schedule list
  agentctl approvals list
  agentctl cost summary
```

## 6.3 Key Commands with Output

### A. Chat (`agentctl chat`)

```bash
$ agentctl chat "Research the latest AI trends and summarize"
```

```
╭──────────────────────────────────────────────────────────────╮
│ 🚀 GOAL SUBMITTED TO SUPERVISOR                             │
│ Session: session-12345                                      │
╰──────────────────────────────────────────────────────────────╯

Task Graph:
┌─────────────────────────────────────────────────────────────┐
│ Goal: Research the latest AI trends and summarize           │
│                                                             │
│  [t1] Research: "Latest AI trends 2026"                    │
│       ├─ Agent: ResearchAgent                              │
│       ├─ Status: [████████░░░░░░░░░░░░] 40%               │
│       └─ LLM: claude-sonnet-5 (est. $0.08)                │
│                                                             │
│  [t2] Summarize findings (depends on t1)                   │
│       ├─ Agent: DocumentAgent                              │
│       └─ LLM: claude-haiku-4-5 (est. $0.01)               │
│                                                             │
│  [t3] Generate PDF (depends on t2)                         │
│       └─ LLM: none (local)                                 │
└─────────────────────────────────────────────────────────────┘

Estimated Cost: $0.09 | Estimated Time: ~3 min

Real-time Output:
┌─────────────────────────────────────────────────────────────┐
│ [t1] ResearchAgent:                                          │
│ 🔍 Searching: "Latest AI trends 2026"                      │
│   → Found 8 relevant sources (1.2s)                         │
│   → Fetching content (3.4s)                                │
│   ✅ Complete: 5 key trends identified                      │
└─────────────────────────────────────────────────────────────┘

[Enter to watch live, 'q' to quit, 'a' to approve next action]
```

**Interactive mode:** press 'a' to show approval queue if any, 'q' to detach (runs in background).

### B. Status (`agentctl status`)

```
╭──────────────────────────────────────────────────────────────╮
│ 📊 SYSTEM STATUS                                             │
│ Service: ✅ Running (uptime: 23h 45m)                       │
│ API: localhost:8000 ✅    Dashboard: localhost:3000 ✅       │
│ Database: ✅ Connected (sqlite)   Redis: ✅ Connected       │
╰──────────────────────────────────────────────────────────────╯

Active Agents:                Task Queue:
│ agent-001 │ Research │ ✅ Running │    Running: 2
│ agent-002 │ Code     │ ✅ Running │    Queued: 5
│ agent-003 │ Document │ ⏳ Waiting  │    Approved (pending): 1
│ agent-004 │ Autopilot│ ⏸️  Paused  │

Cost Summary (today):
│ Spent: $2.34 / $10.00 budget (23%)                         │
│ Projected end-of-day: $3.12   Status: ✅ On track          │

Insights:
├─ 12 tasks completed today
├─ 98% success rate
└─ Most used agent: CodeAgent (35%)
```

### C. Workspace (`agentctl workspace list` / `inspect <id>`)

```
Active Workspaces:
│ ID       │ Name              │ Status   │ Size    │ Modified │
│ ws-0001  │ FastAPI Scaffold  │ ✅ Ready │ 142 KB  │ 5m ago   │
│ ws-0002  │ React Dashboard   │ ✅ Ready │ 2.1 MB  │ 2h ago   │
```

`inspect` shows: container id, file tree, recent commits (signed CodeAgent), QA status (tests/coverage), templates.

### D. Vault Search (`agentctl vault search <query>`)

```
1️⃣  Decision: Better Auth + FastAPI JWT Reconciliation
   Path: vault/Decisions/auth-reconciliation.md
   Tags: [auth, security, backend]   Relevance: 98% ⭐
```

### E. Cost Summary (`agentctl cost summary --period month`)

```
│ Spent: $18.42 / $100.00 budget (18%)                       │
│ Projected (30 days): $92.10 ✅ On track                    │

Breakdown by Agent Type:            Breakdown by LLM Model:
│ ResearchAgent  │ $8.42 (46%)      │ claude-opus-5   │ $8.92 (48%)
│ CodeAgent      │ $5.29 (29%)      │ claude-sonnet-5 │ $6.34 (34%)
│ DocumentAgent  │ $3.15 (17%)      │ claude-haiku-4-5│ $2.81 (15%)

Recommendations:
├─ 💡 Switch more summarization tasks to Claude Haiku → save $1.2/day
├─ 💡 Batch research tasks to reduce overhead → save ~8%
```

### F. Recipes (`agentctl recipes list` / `run <id>`)

```
│ [1] Blog Publishing Pipeline                               │
│     Research topic → Write article → Generate PDF + email  │
│     Parameters: topic (string), email (string)             │
│     Usage count: 12   Duration: ~15 min   Est. cost: $0.45 │
```

Run: `agentctl recipes run 1 --topic "AI Trends 2026" --email "me@example.com"`
Shows live DAG progress, pending approvals inline ('a' to approve, 'q' to quit).

### G. Approvals (`agentctl approvals list`)

- 🔴 HIGH PRIORITY: browser transactional actions (payment submit) with form preview, expiry countdown.
- 🟡 MEDIUM: file deletes with diff preview, browser auth with session note.
- Per action: [Approve] [Deny] [Always allow this type].

### H. Logs (`agentctl logs --follow --agent ResearchAgent`)

Streaming log lines with timestamps, agent id, level; ends with cost/duration/tokens per task.

## 6.4 Interactive Prompts & Input

Multi-line chat input (Ctrl+D to submit). Shell autocompletion for subcommands:

```bash
$ agentctl workspace [TAB]
  list | inspect | new | clone | delete | run | save-template
```

## 6.5 Output Formats

```bash
$ agentctl status --json        # machine-readable JSON (for scripting)
$ agentctl cost summary --csv > costs.csv
```

## Related

- Dashboard UI: [[16_Dashboard_UIUX]]
- API: [[18_API_Reference]]


---

---
title: API Reference
type: reference
project: Agent System
status: locked
updated: 2026-09-06
up: "[[00_Index]]"
---

# API Reference

> Consolidated from Spec v3.0 §4. Base: `http://localhost:8000`. Dashboard: `http://localhost:3000`.
>
> [!warning] v3.1 API Contract
> All production endpoints are **versioned under `/api/v1/...`** ([[23_Engineering_Contract]]). Every endpoint defines: method, path, request/response/error schemas, status codes, authentication, authorization, pagination/filtering where needed, idempotency where needed. No undocumented production endpoints; docs generated automatically where possible. The v3.0 paths below are grouped by domain and prefixed with `/api/v1` at implementation time.

## System (v3.1 §32 — [[28_Reliability_Operations]])

```
GET /api/v1/health        # liveness
GET /api/v1/ready         # readiness (DB, Redis, providers)
```

## Events & Realtime (v3.1 §6, §17 — [[25_Event_System]])

```
GET /api/v1/events?after_sequence=123   # resume missed events (ordering, dedupe by event_id)
WS  /api/v1/events/stream               # authenticated event stream (heartbeat, sequence numbers)
SSE /api/v1/events/stream               # SSE equivalent; backpressure-safe
```

## Reasoning (Feature 1/11 → [[05_Feature_Reasoning_Trace_Viewer]])

> v3.1: renamed **Decision & Execution Trace** — auditable events only (plan/decision summaries, tool calls, state transitions), never private chain-of-thought.

```
WS  /api/v1/ws/reasoning/{session_id}
    -> {type: "token", content, logprob, timestamp}
    -> {type: "decision", from_node, options, chosen}
GET /api/reasoning/{session_id}          # full trace after completion
```

## Models & Cost (Feature 2/6 → [[06_Feature_Multi_Model_Orchestration]], [[10_Feature_Cost_Optimizer]])

```
GET  /api/models                          # {available, current_selection}
PUT  /api/models/{agent_type}/prefer      # {mode: fastest|cheapest|capable, apply_to_all}
GET  /api/cost/summary?period=day|week|month
GET  /api/cost/status                     # {period, limit, spent, remaining, pct_used, trend}
GET  /api/cost/breakdown?group_by=agent|model|task
GET  /api/cost/recommendations            # [{action, savings_usd, impact_description}]
POST /api/cost/budget                     # {period, limit_usd, alert_threshold_pct}
```

## Errors & Recovery (Feature 3 → [[07_Feature_Error_Recovery]])

```
GET  /api/errors?agent_type=code&limit=50
GET  /api/errors/patterns
POST /api/errors/{id}/retry
```

## Workspace & Templates (Feature 4 → [[08_Feature_Workspace_Templates]])

```
POST   /api/workspace/{id}/save-template  # {name, description, tags}
GET    /api/templates
POST   /api/workspace/clone-from-template/{template_id}
DELETE /api/templates/{id}
```

## Recordings & Replay (Feature 5 → [[09_Feature_Behavior_Recording]])

```
GET  /api/recordings/{session_id}
GET  /api/recordings/{session_id}/replay/step/{seq}
POST /api/recordings/{session_id}/replay/resume-from/{seq}
```

## Tasks & Batching (Feature 7 → [[11_Feature_Task_Batching]])

```
GET  /api/tasks/batching-config           # {enabled, similarity_threshold, min_batch_size}
PUT  /api/tasks/batching-config
GET  /api/tasks/batches?limit=20
```

## QA Reports (Feature 8 → [[12_Feature_Autonomous_QA]])

```
GET  /api/qa/reports?task_id=...
GET  /api/qa/reports/{id}/html
```

## Recipes (Feature 9 → [[13_Feature_Recipe_Library]])

```
GET  /api/recipes
POST /api/recipes                         # {name, description, task_dag, parameters}
POST /api/recipes/{id}/execute            # {param_values} -> session_id
GET  /api/recipes/{id}/executions
```

## Agents & Personality (Feature 10 → [[14_Feature_Personality]])

```
GET  /api/agents/{id}/personality
PUT  /api/agents/{id}/personality
POST /api/agents/{id}/feedback            # {rating, comment}
```

## Insights (Feature 12 → [[15_Feature_Insight_Generation]])

```
GET  /api/insights?type=daily_briefing&limit=7
GET  /api/insights/{id}
POST /api/insights/config                 # {enabled, frequency, delivery}
```

## Realtime Streams (v3.0 paths — normalized to the v3.1 events contract above)

| Channel | Protocol | Payload |
| --- | --- | --- |
| Reasoning tokens | WS `/api/v1/ws/reasoning/{session_id}` | token/decision events |
| Task progress | WS `/api/v1/ws/tasks/{session_id}` | status/progress updates |
| Chat tokens | SSE `/api/v1/sse/chat/{session_id}` | token deltas |
| Logs | WS `/api/v1/ws/logs` | log lines |

All streams: authentication, heartbeat, reconnect, sequence numbers, ordering, duplicate handling, resume-from-sequence ([[25_Event_System]]).

## Conventions

- Auth: explicit session secret — localhost-only bind but still authenticated ([[27_Security_Permissions]]).
- Errors: `{"error": {"code", "message", "details"}}` with proper HTTP status codes, documented per endpoint.
- IDs: prefixed ULID strings per [[24_Canonical_Domain_Model]]. Timestamps: UTC ISO-8601.
- Idempotency: retryable operations accept an idempotency key ([[28_Reliability_Operations]]).
- Pagination/filtering: required for list endpoints (events, tasks, logs, errors, costs).


---

---
title: Execution Plan
type: plan
project: Agent System
status: locked
updated: 2026-09-06
up: "[[00_Index]]"
---

# Execution Plan — v3.1 (Phases 0–19)

> **Source:** v3.1 §41 · Supersedes the v3.0 16-phase plan. Incremental; each phase has acceptance criteria.
> Gate rule ([[23_Engineering_Contract]]): **do not advance** with failing critical tests, broken migrations, security violations, unresolved data corruption, fake production functionality, or undocumented divergence. After each phase: tests → build → integration tests → update status docs → descriptive Git commit.

## Phase 0 — Repository Forensics
Inventory the entire system before modifying code ([[30_Implementation_Docs]]: REPOSITORY_INVENTORY).
**Accept:** repository inventory · architecture map · dependency map · test baseline.

## Phase 1 — Domain + Persistence
Canonical entities & IDs ([[24_Canonical_Domain_Model]]) · SQLite config (WAL, FKs, busy timeout) · Alembic migrations · repositories.
**Accept:** migrations pass · CRUD tests pass · restart persistence verified.

## Phase 2 — Event System
Canonical events ([[25_Event_System]]): persistence, ordering, sequence numbers, dedup.
**Accept:** event persistence · ordering · sequence numbers · duplicate handling · tests.

## Phase 3 — API + Permissions
API v1 ([[18_API_Reference]]) · authentication · permission gate · approvals ([[27_Security_Permissions]]).
**Accept:** unauthorized actions rejected · approvals persisted · API contract tests pass.

## Phase 4 — Orchestrator + Queue
Supervisor · task DAG · worker · agent lifecycle ([[26_Task_Agent_Lifecycles]]) · cancellation · recovery.
**Accept:** multi-step task succeeds · worker crash recovery succeeds.

## Phase 5 — Sandbox + Workspace
Secure workspace execution (Docker isolation, resource limits).
**Accept:** isolation tests · resource limits · workspace persistence · secret exclusion.

## Phase 6 — Browser + Research
Browser/research agents (Playwright), isolated sessions.
**Accept:** isolated browser sessions · permission checks · recording · failure recovery.

## Phase 7 — Documents
PPTX · DOCX · PDF · XLSX ([[20_Deployment]] deps).
**Accept:** generated files valid · artifact persistence · sandbox execution.

## Phase 8 — Memory + Vault
Obsidian integration · LanceDB · memory lifecycle & retrieval · memory layers ([[28_Reliability_Operations]] §Memory).
**Accept:** persistence · retrieval · secret filtering.

## Phase 9 — Dashboard
Chat · Kanban · Workspace · Vault · Outputs · Schedule · Approvals · Templates · Cost · Recipes · Reasoning · Insights · Audit · Settings ([[16_Dashboard_UIUX]]).
**Accept:** all production UI uses real APIs — no mocked functionality (v3.1 §33).

## Phase 10 — CLI
`agentctl` ([[17_CLI_Specification]]) using the same `/api/v1` contracts.
**Accept:** every implemented major operation accessible · JSON mode · stable exit codes · interactive mode.

## Phase 11 — Model Router + Cost
Provider registry · model selection · pricing registry · cost tracking · budgets ([[06_Feature_Multi_Model_Orchestration]], [[10_Feature_Cost_Optimizer]]).
v3.1 rules: provider adapters (no hardcoded fictional model IDs), provider-aware token metrics, unknown cost never crashes, budget levels 50/75/90/100%, per-task/session/daily/provider budgets ([[28_Reliability_Operations]]).
**Accept:** every invocation produces a ModelCall record · cost marked estimated/unknown explicitly · budget alerts fire.

## Phase 12 — Error Recovery
Classification · retry · recovery planner/executor · pattern learning ([[07_Feature_Error_Recovery]]).
v3.1 rules: never auto-retry destructive actions, permission failures, deterministic validation errors, or repeated identical failures; max retries + exponential backoff ([[28_Reliability_Operations]]).
**Accept:** classified failures recovered safely · patterns persisted · retry limits enforced.

## Phase 13 — QA
Autonomous QA ([[12_Feature_Autonomous_QA]]). Generated tests are **untrusted code** — run in sandbox with no host FS, no host credentials, restricted network, CPU/memory limits, timeout.
**Accept:** structured reports (generated/executed/passed/failed/skipped/duration/coverage/diagnostics) · sandboxed execution verified.

## Phase 14 — Templates
Workspace template system ([[08_Feature_Workspace_Templates]]).
v3.1 rules: templates never contain `.env`, credentials, API keys, private keys, browser sessions, or secret config; record template version + source metadata.
**Accept:** secret exclusion verified · versioned templates · clone fidelity.

## Phase 15 — Recording + Replay
Inspect · simulate · approved re-execute ([[09_Feature_Behavior_Recording]]).
v3.1 rules: never blindly re-execute historical actions; before re-execution compare workspace fingerprint, OS, dependencies, agent version, model config, recipe version, permissions; destructive/network/payment re-execution requires explicit approval.
**Accept:** all three modes boundary-tested · replay safety verified.

## Phase 16 — Batching + Recipes
Task batching ([[11_Feature_Task_Batching]]) · recipe execution ([[13_Feature_Recipe_Library]]).
v3.1 rules: batch only compatible tasks (agent type, workspace, permissions, dependencies, resources); recipes versioned, run through the canonical Task/Event system, never bypass permissions.
**Accept:** batch_id + idempotency · cancellation · partial failure · per-task results · recipe versioning.

## Phase 17 — Personality + Insights
Versioned personality · feedback ([[14_Feature_Personality]]) · insight generation ([[15_Feature_Insight_Generation]]).
v3.1 rules: personality never modifies security/permission policies or safety constraints; insights derived only from canonical persisted events, respect secret redaction, no fabricated analytics.
**Accept:** personality versioned & auditable · feedback loop works · insights sourced from events.

## Phase 18 — Autopilot (LAST)
Desktop automation only after permission system, audit, recording, resource limits, and recovery all work ([[27_Security_Permissions]]).
**Accept:** disabled by default · own permission boundary · fully audited.

## Phase 19 — Production Hardening
Full integration tests · security audit · chaos tests ([[29_Testing_Strategy]]) · migration tests · performance tests · UI smoke tests · CLI tests · restart tests · documentation audit ([[30_Implementation_Docs]]).
**Accept:** E2E acceptance lifecycle passes after clean install/restart · FINAL_REPORT.md written with honest completion labels.


---

---
title: Deployment
type: runbook
project: Agent System
status: locked
updated: 2026-09-06
up: "[[00_Index]]"
---

# 9. Deployment

> **Source:** Spec v3.0 §9

## Install

```bash
# Full stack
uv venv && uv pip install -r requirements.txt
playwright install --with-deps
# pytesseract setup (macOS: brew install tesseract; Linux: apt-get install tesseract-ocr)
cp .env.example .env
docker compose up -d redis

# DB
alembic upgrade head

# Service
systemctl --user enable agent-system.service
systemctl --user start agent-system.service

# Dashboard + API
npm run build && npm start   # localhost:3000
# API on localhost:8000
```

## Environment Variables (`.env.example`)

| Var | Purpose |
| --- | --- |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | Primary LLM providers |
| `REDIS_URL` | Default `redis://localhost:6379/0` |
| `DATABASE_URL` | Default SQLite file; Postgres optional |
| `VAULT_PATH` | Absolute path to Obsidian vault |
| `WORKSPACES_DIR` | Where coding workspaces live |
| `TEMPLATES_DIR` | Workspace template snapshots |
| `RECORDINGS_DIR` | Behavior recording `.jsonl` files |
| `OUTPUTS_DIR` | Generated documents |
| `DAILY_BUDGET_USD` | Default cost budget |
| `API_PORT` / `WEB_PORT` | Default 8000 / 3000 |

## Service Management

| OS | Mechanism |
| --- | --- |
| Linux | `systemctl --user` unit `agent-system.service` |
| macOS | `launchd` plist (LoadAgent) |
| Windows | NSSM service wrapper |

## Health Checks

- API: `GET http://localhost:8000/api/health`
- Dashboard: `http://localhost:3000` loads
- Redis: `redis-cli ping` → PONG
- DB: `alembic current` matches head
- CLI: `agentctl status` all-green panel

## Packaging

`uv + PyInstaller` for one-command install of the CLI binary (v3).

## Related

- [[03_Tech_Stack]] · [[19_Execution_Plan]]


---

---
title: Progress Log
type: log
project: Agent System
status: active
updated: 2026-09-06
up: "[[00_Index]]"
---

# Agent System — Progress Log

> **Status:** Implementation phase · **Owner:** Ashutosh · **Spec:** v3.0 vision + v3.1 contract
> Living log — update after every work session. Decisions go to [[22_Decision_Log]].
> Implementation status mirrors `agent-system/docs/implementation/STATUS.md` + `AGENT_STATE.md` ([[30_Implementation_Docs]]).
>
> **Verified 2026-09-06 (final build pass):** 255 passed / 1 env-gated fail (Docker daemon) / 5 skipped, ruff clean, mypy --strict clean (44 files), next build ✓ 17 routes. **All 20 phases implemented.** Tracker below reflects code reality.

## Phase Tracker (v3.1 — [[19_Execution_Plan]])

| Phase | Scope | Status | Notes |
| --- | --- | --- | --- |
| 0 | Repository forensics | ✅ Done | REPOSITORY_INVENTORY.md, tooling, baseline |
| 1 | Domain + persistence | ✅ Done | 22-table schema, migration `94be8eadb99f`, restart tests |
| 2 | Event system | ✅ Done | EventBus + WS/SSE fanout + idempotent event_id dedupe, resume tests |
| 3 | API v1 + permissions | ✅ Done | auth, PermissionGate, approvals, contract tests |
| 4 | Orchestrator + queue | ✅ Done + live-verified | Redis up; out-of-process exec ✓; kill -9 → reaper recovery ✓; attempt double-increment fixed |
| 5 | Sandbox + workspace | ✅ Done | DockerSandbox + traversal-safe WorkspaceManager; sandbox test needs Docker daemon |
| 6 | Browser + research | ✅ Core done | `agents/browser_research.py` (Playwright optional, graceful w/o deps) |
| 7 | Documents | ✅ Done | DocumentAgent PPTX/DOCX/XLSX/PDF + artifacts |
| 8 | Memory + vault | ✅ Core done | Obsidian writer + hashed-lexical embeddings; LanceDB swap-in pending |
| 9 | Dashboard | ✅ Core done | 12/14 tabs wired to live APIs (Chat…Schedule); Vault+Templates honest placeholders |
| 10 | CLI | ✅ Core done | `agentctl` (sessions/tasks/approvals/workspaces/events), contract tests |
| 11 | Model router + cost | ✅ Core done | ModelRouter/Pricing/BudgetMonitor; cost UI tab pending |
| 12 | Error recovery | ✅ Done | classify→plan→execute→learn pipeline + tests |
| 13 | QA | ✅ Core done | untrusted-test sandbox + subprocess fallback; Docker-gated test fails w/o daemon |
| 14 | Templates | ✅ Done | TemplateManager + secret scanning (Phase 5 core) |
| 15 | Recording + replay | ✅ Done | BehaviorRecorder + INSPECT/SIMULATE/APPROVED_REEXECUTE + fingerprints; 15 tests |
| 16 | Batching + recipes | ✅ Done | TaskBatcher (partial-failure isolation) + RecipeEngine (versioned DAGs); 19 tests |
| 17 | Personality + insights | ✅ Done | security-bounded personality + event-derived insights + scheduler; 8 tests |
| 18 | Autopilot (last) | ✅ Done (service) | off by default, per-action approvals, kill switch, audit; 17 security tests |
| 19 | Production hardening | ✅ Core done | chaos/restart suite + FINAL_REPORT.md; live-infra chaos needs Redis/Docker |

Legend: ⬜ Not started · 🟨 In progress · ✅ Done · ⏸️ Paused

---

## 2026-09-06 — Project Initialized

- Created this documentation vault folder `Agent_System/` with full v3.0 spec broken into linked notes.
- Indexed all 12 unique features, data model, UI/UX, CLI, API, execution plan, deployment.

## 2026-09-06 — RQ Worker Live-Verified (Redis up, kill -9 recovery proven)

- `docker compose up -d` → redis:7-alpine healthy on :6379.
- Worker now registers with unique per-process names — a crashed worker's stale Redis registration previously blocked restart (`ValueError: active worker named 'agent-worker' already`).
- **E2E happy path** (`scripts/e2e_worker_check.py`): task QUEUED → RQ enqueue → worker executed out-of-process → SQLite SUCCEEDED, AgentRun COMPLETED with distinct worker_id, lease cleaned, full event chain.
- **E2E crash recovery** (`scripts/e2e_crash_recovery_check.py`): kill -9 mid-RUNNING → lease expired → `recover_orphans` reaped + RECOVERING→QUEUED, no duplicate agent_runs, `recovery.*` events emitted.
- **Bug found & fixed by E2E:** `attempt` was double-incremented (worker at RUNNING + reaper/retry on requeue). Corrected invariant: attempt counts "times execution started," incremented only at RUNNING transitions (incl. manual API). Unit tests updated — they had passed only because each path was tested in isolation.
- Suite: **260 passed**, 1 Docker-gated fail; ruff + mypy clean.

## 2026-09-06 — Phases 15–19 Implemented (final build pass)

- **Phase 15:** BehaviorRecorder (.jsonl, secret-scrubbed), RecordingContext, ReplayService with INSPECT/SIMULATE/APPROVED_REEXECUTE + ReplayContext fingerprints; `/recordings` + `/recordings/{id}/replay` APIs (403 on blocked re-execution).
- **Phase 16:** TaskBatcher (compatibility checks, partial-failure isolation, cancel, speedup) + RecipeEngine (versioned validated DAGs, `{{param}}` substitution, canonical Supervisor pipeline); `/batches` + `/recipes` APIs.
- **Phase 17:** PersonalityManager (versioned, rejects non-adjustable fields, N=10 prompt-only learning) + InsightGenerator (only canonical events, honest-empty, anomaly suggestions) + `/schedule` persisted jobs; `/personality` + `/insights` APIs.
- **Phase 18:** AutopilotService — off by default, per-action fresh approval binding, hard default-deny list, caps, sticky kill switch, full audit; executor injected at composition. `/autopilot/status|kill|reset`.
- **Phase 19:** chaos/restart suite (event dedupe, double delivery, crash-reopen, restart, approval expiry, replay-safety, batch isolation, worker idempotency) + FINAL_REPORT.md. EventBus emit made idempotent (real dedupe fix found by chaos tests).
- **Phase 9 wiring:** Recipes/Insights/Cost/Reasoning/Schedule tabs now live; Vault/Templates honest placeholders; frontend builds ✓.
- Toolchain: ruff clean, mypy --strict clean (44 files), 255 tests passing (1 Docker-gated fail).
- **Remaining:** Redis+Docker runtime verification, Vault/Templates list APIs, E2E acceptance (§7).

## 2026-09-06 — Implementation Status Verified (memory sync)

- Audited `agent-system/` against the master plan; refreshed the tracker above.
- Phases 0–7 core implemented (domain, events, API+gate, orchestrator+RQ worker, sandbox/workspaces/templates, browser/research, documents); memory (8) core done with deterministic embeddings; CLI (10) and QA (13) core done; recovery (12) done; router/cost (11) core done.
- Not started: 15 recording/replay, 16 batching/recipes, 17 personality/insights, 18 autopilot, 19 hardening. Dashboard (9) has shell + tabs but most feature tabs await their backend endpoints.
- Environment notes: Redis absent (RQ worker code + docker-compose ready); Docker daemon absent (sandbox/QA Docker tests skip or fail env-gated). Frontend deps installed, not yet built/verified end-to-end.
- **Next:** Phase 15 (recording+replay) → 16 → 17, or dashboard wiring for already-implemented services; Phase 4 RQ worker needs `docker compose up redis`.

## 2026-09-06 — Spec Baseline Upgraded to v3.1

- Integrated the v3.1 engineering contract into the vault: 8 new notes ([[23_Engineering_Contract]] → [[30_Implementation_Docs]]).
- Rewrote [[19_Execution_Plan]] to the v3.1 Phase 0–19 plan with per-phase acceptance criteria and the phase gate.
- Updated [[04_Data_Model]] (SQLite-authoritative storage, v3.1 tables: events, model_calls, tool_calls, agent_leases, idempotency_keys) and [[18_API_Reference]] (`/api/v1` versioning, `/health` + `/ready`, events endpoint).
- Annotated all 11 feature notes with v3.1 refinements (auditable trace, provider-aware cost, replay safety, untrusted-QA sandboxing, permission-bound personality/insights).
- **Next:** Begin [[19_Execution_Plan]] Phase 0 — Repository Forensics.

---


---

---
title: Decision Log
type: log
project: Agent System
status: active
updated: 2026-09-06
up: "[[00_Index]]"
---

# Agent System — Decision Log (ADR)

> Record every locked decision that changes the spec. Format: context → decision → consequences.

## ADR-001 — Lock Tech Stack to Python/FastAPI + Next.js 15

- **Date:** 2026-09-06
- **Context:** Need async orchestrator, rich Python AI ecosystem, familiar frontend.
- **Decision:** Python 3.12 + FastAPI backend; Next.js 15 + Tailwind + shadcn/ui frontend. See [[03_Tech_Stack]].
- **Consequences:** Rust CLI deferred to v3 wrapping the same HTTP API; no native desktop UI.

## ADR-002 — SQLite First, Postgres Optional

- **Date:** 2026-09-06
- **Context:** Zero-config local-first system.
- **Decision:** SQLite default via Alembic migrations; Postgres swap-in optional.
- **Consequences:** Avoid Postgres-specific features in queries; keep JSON columns portable.

## ADR-003 — LanceDB for Vectors

- **Date:** 2026-09-06
- **Context:** No external vector server desired.
- **Decision:** Embedded LanceDB for semantic memory alongside the Obsidian vault.
- **Consequences:** Vault notes are the human-readable layer; LanceDB the machine layer.

## ADR-004 — Process-Isolated Subagents

- **Date:** 2026-09-06
- **Context:** Agent crashes must not take down the orchestrator.
- **Decision:** Each subagent runs as an isolated process with asyncio supervision.
- **Consequences:** IPC overhead accepted; restart semantics required per agent type.

## ADR-005 — Approval Gate Before Risky Actions

- **Date:** 2026-09-06
- **Context:** Autopilot and browser actions can have real-world effects.
- **Decision:** Permission gate intercepts scoped actions; high-risk scopes always queue for human approval.
- **Consequences:** Adds latency for gated actions; audit log records every decision.

## ADR-006 — Obsidian Vault as Human Memory Layer

- **Date:** 2026-09-06
- **Context:** Memory must be dual-use (agent + human readable).
- **Decision:** Markdown + YAML frontmatter notes in the user's Obsidian vault; `[[wiki-links]]` for relationships.
- **Consequences:** Agent writes must respect vault conventions; sync conflicts surfaced in Vault tab.

## ADR-007 — Adopt v3.1 as the Engineering Contract

- **Date:** 2026-09-06
- **Context:** v3.0 defines WHAT to build but under-specifies reliability, security, and verification. Autonomous implementation needs an enforceable contract.
- **Decision:** Adopt v3.1 ([[23_Engineering_Contract]]) as governing contract: source-of-truth hierarchy, phase gate, Definition of Done, no-shortcut policy, frontend/CLI real-backend rules, layered dependency rules.
- **Consequences:** Smaller fully-functional feature set preferred over breadth; every feature requires persistence + tests + restart verification before "done".

## ADR-008 — SQLite Authoritative, Redis Ephemeral

- **Date:** 2026-09-06
- **Context:** v3.0 implied Redis-backed state; crash recovery requires durable truth.
- **Decision:** SQLite (WAL, FKs, busy timeout) is the authoritative store; Redis is queue/cache/coordination only ([[28_Reliability_Operations]]).
- **Consequences:** All state reconstructible after Redis loss; slightly more write traffic to SQLite.

## ADR-009 — Canonical Event System as Single Audit Surface

- **Date:** 2026-09-06
- **Context:** Traces, recordings, insights, and audit were speced as separate pipelines risking divergence.
- **Decision:** One event bus with the v3.1 envelope and catalog ([[25_Event_System]]); traces/recordings/insights/audit all derive from it.
- **Consequences:** Single ordering/dedup/resume implementation; features render persisted events instead of private streams.

## ADR-010 — Reasoning Trace Renamed & Scoped to Auditable Data

- **Date:** 2026-09-06
- **Context:** v3.0 promised token-level "reasoning" that providers may not expose and chain-of-thought may be private.
- **Decision:** Rename to **Decision & Execution Trace**; capture auditable events only; token data optional/provider-dependent ([[05_Feature_Reasoning_Trace_Viewer]]).
- **Consequences:** No false claims in UI; trace works uniformly across providers.

## ADR-011 — Security Defaults

- **Date:** 2026-09-06
- **Context:** Autonomous agents with host access, browsers, and payments are high-risk.
- **Decision:** Centralized permission gate with 4 risk levels and 5 policies; dangerous operations default-deny; autopilot disabled by default and built last ([[27_Security_Permissions]]).
- **Consequences:** Some friction on risky actions; audit records every decision; autopilot ships in Phase 18 only after audit/recording/recovery mature.

---


---

---
title: v3.1 Engineering Contract
type: contract
project: Agent System
status: active
updated: 2026-09-06
up: "[[00_Index]]"
---

# v3.1 — Autonomous Implementation & Reliability Specification (Engineering Contract)

> v3.0 ([[01_Overview]]) is the **product vision**. This note is the **engineering contract**.
> On conflict, the source-of-truth hierarchy below wins.

## Source-of-Truth Hierarchy

1. Existing repository reality
2. This v3.1 engineering contract
3. Existing v3.0 product requirements
4. Existing tests
5. Existing documentation
6. Existing implementation assumptions

Before changing architecture, inspect the repository. Never assume v3.0's proposed files already exist; never duplicate an existing subsystem when it can safely be extended. If architecture conflicts with this contract: document the conflict, choose the safest coherent migration path, preserve functionality, update docs, add migration steps, test the migration.

## Primary Objective

Build a system that is: locally deployable · persistent · observable · recoverable · secure by default · modular · testable · restart-safe · permission-aware · cost-aware · multi-agent · extensible · UI-accessible · CLI-accessible · genuinely autonomous within defined boundaries.

Do not optimize for feature count. Optimize for: correctness, reliability, security, recoverability, maintainability, observability, integration, UX. **A smaller fully functional system beats a huge collection of fake features.**

## Autonomous Execution Policy

Make reasonable engineering decisions autonomously; do not stall on routine choices (naming, tests, refactors, documentation). Record non-trivial assumptions in the Decision Log ([[22_Decision_Log]]) and [[30_Implementation_Docs]] statuses.

Stop only when genuinely blocked by something that cannot safely be inferred: missing production credentials, unavailable external service, or destructive irreversible external action requiring human authorization.

When blocked:
1. Implement everything possible without the dependency.
2. Create a clean adapter/interface.
3. Add configuration placeholders.
4. Add tests using deterministic local test doubles.
5. Document the exact blocker.
6. Continue with all unrelated work.

**Never use missing credentials as an excuse to leave the architecture incomplete.**

## Phase Gate

Do **not** move to the next phase if the current phase has: failing critical tests, broken migrations, security violations, unresolved data corruption, fake production functionality, or undocumented architectural divergence.

After each successful phase: run tests → run build → run integration tests → update status notes ([[30_Implementation_Docs]], [[21_Progress_Log]]) → create a descriptive Git commit. Never force-reset user work, delete unknown user files, or rewrite unrelated history.

## Definition of Done

A feature is NOT complete because: code compiles, endpoint exists, UI renders, tests were skipped, mock data works.

A feature is complete only when:
1. Implementation exists
2. Integration exists
3. Persistence works
4. Error handling exists
5. Security boundaries exist
6. API contract exists
7. UI/CLI integration exists where applicable
8. Tests exist
9. Restart behavior is verified
10. Documentation is updated
11. No fake production behavior exists
12. Phase acceptance criteria pass

## No-Shortcut Policy

Never solve implementation problems by: commenting out failing code, weakening tests, deleting features, replacing real functionality with mocks, hardcoding success, swallowing exceptions, hiding errors, disabling security checks, bypassing permissions, adding TODOs instead of required behavior, returning fake API responses, embedding fake analytics, or creating placeholder backend routes.

If something cannot be implemented safely: **document it explicitly.**

## Frontend & CLI Rules

- **Frontend ([[16_Dashboard_UIUX]]):** must consume real backend contracts. No fake costs, agents, reasoning, approvals, task status, replay, analytics, downloads, or progress. Fixtures allowed only in tests/development.
- **CLI ([[17_CLI_Specification]]):** must use the same API/domain contracts as the web dashboard. Support `--json`, `--verbose`, `--no-color`. JSON output machine-readable and stable. No separate CLI-only business logic.

## Architectural Dependency Rules

```
API
 ↓
Application Services
 ↓
Domain
 ↓
Infrastructure
```

Do NOT allow: Frontend → database · Agent → raw SQL · Agent → host Docker socket · Feature → another feature's private tables · UI → direct filesystem mutation · CLI → duplicated business logic. Use repositories/services/interfaces.

## Documentation Contract

Maintain living implementation docs (see [[30_Implementation_Docs]]): ARCHITECTURE, API, EVENTS, RECOVERY, SECURITY, THREAT_MODEL, DECISIONS, STATUS, OPERATIONS, TROUBLESHOOTING, REPOSITORY_INVENTORY, AGENT_STATE, FINAL_REPORT. Documentation must evolve with implementation — never knowingly describe behavior the implementation does not have.

## Final Directives

CORRECTNESS > COMPLETENESS > CLEVERNESS · RELIABILITY > FEATURE COUNT · SECURITY > CONVENIENCE · REAL IMPLEMENTATION > MOCKS · OBSERVABILITY > MAGIC · RECOVERY > ASSUMPTIONS · DOCUMENTED BEHAVIOR > IMPLICIT BEHAVIOR.

The final system must be something another engineer can clone, start, inspect, debug, recover, extend, and trust.


---

---
title: Canonical Domain Model
type: spec
project: Agent System
status: locked
updated: 2026-09-06
up: "[[00_Index]]"
---

# Canonical Domain Model (v3.1 §5, §35)

> One canonical domain model shared by the whole system. No subsystem may invent incompatible identifiers for the same underlying object.

## Core Entities

| Entity | Identifier | Purpose |
| --- | --- | --- |
| Session | `session_id` | A user goal being pursued end-to-end |
| Task | `task_id` | A unit of work in the supervisor's DAG |
| AgentRun | `agent_run_id` | One execution of an agent on a task |
| Agent | (registry entry) | Agent type/instance definition |
| Event | `event_id` | Canonical state transition (see [[25_Event_System]]) |
| ToolCall | `tool_call_id` | One tool invocation by an agent |
| ModelCall | `model_call_id` | One LLM invocation (see [[06_Feature_Multi_Model_Orchestration]]) |
| Approval | `approval_id` | Permission-gate decision record (see [[27_Security_Permissions]]) |
| Workspace | `workspace_id` | Sandboxed coding workspace |
| Artifact | `artifact_id` | Generated file/document |
| Memory | `memory_id` | Memory entry (see [[28_Reliability_Operations]] §Memory) |
| Recording | `recording_id` | Behavior recording for replay (see [[09_Feature_Behavior_Recording]]) |
| Recipe | `recipe_id` | Versioned workflow (see [[13_Feature_Recipe_Library]]) |
| CostRecord | (in ModelCall/Task) | Provider-aware cost accounting (see [[10_Feature_Cost_Optimizer]]) |
| QAReport | `qa_report_id` | Structured test report (see [[12_Feature_Autonomous_QA]]) |
| Insight | `insight_id` | Derived analytics (see [[15_Feature_Insight_Generation]]) |

## ID Conventions

- Prefix + ULID: `ses_…`, `task_…`, `run_…`, `evt_…`, `approval_…`, `ws_…`, etc.
- All timestamps UTC ISO-8601. All JSON validated with Pydantic at boundaries.
- Events carry the full identifier context: `session_id`, `task_id`, `agent_run_id` — this is also the tracing key set for logs (see [[28_Reliability_Operations]]).

## Layered Architecture

```
API
 ↓
Application Services
 ↓
Domain
 ↓
Infrastructure
```

Forbidden dependencies:

- Frontend → database
- Agent → raw SQL
- Agent → host Docker socket
- Feature → another feature's private database tables
- UI → direct filesystem mutation
- CLI → duplicated business logic

Enforce via repositories/services/interfaces. The CLI and dashboard consume the same `/api/v1` contracts ([[18_API_Reference]]).

## Relation to v3.0 Tables

The v3.0 relational tables ([[04_Data_Model]]) are the persistence of these entities; where a v3.0 table lacks an entity identifier above (e.g., tool calls, model calls, events), extend the schema via Alembic migration rather than inventing parallel identifiers.


---

---
title: Canonical Event System
type: spec
project: Agent System
status: locked
updated: 2026-09-06
up: "[[00_Index]]"
---

# Canonical Event System (v3.1 §6, §17)

> One event bus. Every important state transition produces an event. Do not create multiple incompatible event buses.

## Event Envelope

```json
{
  "event_id": "evt_...",
  "schema_version": 1,
  "session_id": "ses_...",
  "task_id": "task_...",
  "agent_run_id": "run_...",
  "sequence": 123,
  "timestamp": "2026-09-06T12:00:00.000Z",
  "type": "task.started",
  "actor": "supervisor",
  "payload": {},
  "visibility": "user",
  "sensitivity": "normal"
}
```

Events must support: ordering · deduplication · replay · audit · WebSocket delivery · SSE delivery · persistence · recovery · analytics.

## Canonical Event Catalog

| Domain | Events |
| --- | --- |
| Session | `session.created`, `session.completed` |
| Task | `task.created`, `task.started`, `task.queued`, `task.completed`, `task.failed`, `task.cancelled`, `task.recovering`, `task.blocked_approval` |
| Agent | `agent.created`, `agent.started`, `agent.waiting_tool`, `agent.waiting_approval`, `agent.completed`, `agent.failed`, `agent.terminated` |
| Model | `model.requested`, `model.completed`, `model.failed` |
| Tool | `tool.started`, `tool.completed`, `tool.failed` |
| Approval | `approval.requested`, `approval.approved`, `approval.denied`, `approval.expired` |
| Workspace | `workspace.created`, `workspace.modified`, `workspace.destroyed` |
| Artifact | `artifact.created`, `artifact.deleted` |
| QA | `qa.started`, `qa.completed`, `qa.failed` |
| Recovery | `recovery.started`, `recovery.completed`, `recovery.failed` |
| Recipe | `recipe.started`, `recipe.completed`, `recipe.failed` |
| Cost | `cost.recorded` |
| Insight | `insight.generated` |

State transitions themselves are governed by [[26_Task_Agent_Lifecycles]]; events are the audit surface of those transitions.

## Realtime Delivery Contract (WS/SSE)

- **Authentication** required for streams.
- **Heartbeat** + **reconnect** semantics.
- **Sequence numbers** on every event; **event ordering** guaranteed per session.
- **Duplicate handling** (client dedupes by `event_id`).
- **Resume-from-sequence:** `GET /api/v1/events?after_sequence=123` replays missed events so the frontend reconnects without losing important events.
- **Backpressure:** slow clients must not block the orchestrator.

## Persistence

Events are appended to SQLite (authoritative) and fanned out in-memory to WS/SSE subscribers. Redis may carry ephemeral fanout but is never the durable record (see [[28_Reliability_Operations]] §Storage).

## Feature Integration

- Reasoning/Decision & Execution Trace renders these events ([[05_Feature_Reasoning_Trace_Viewer]]).
- Behavior Recording is a persisted, replayable event stream ([[09_Feature_Behavior_Recording]]).
- Insights are derived from canonical persisted events only — no fabricated analytics ([[15_Feature_Insight_Generation]]).


---

---
title: Task & Agent Lifecycles
type: spec
project: Agent System
status: locked
updated: 2026-09-06
up: "[[00_Index]]"
---

# Task & Agent Lifecycles (v3.1 §7, §8, §9)

## Task State Machine

States:

```
PENDING → PLANNING → QUEUED → RUNNING → SUCCEEDED
                              ↓ ↘
                 BLOCKED_APPROVAL  REVIEW
                              ↓        ↘
                          RECOVERING    SUCCEEDED/FAILED
                              ↓
                        RUNNING/FAILED
```

Full allowed set: `PENDING, PLANNING, QUEUED, RUNNING, BLOCKED_APPROVAL, RECOVERING, REVIEW, SUCCEEDED, FAILED, CANCELLED`.

**Rules:**
- Define an explicit transition table; **invalid transitions must be rejected** (raise, never silently coerce).
- Every transition must: 1) validate, 2) persist state, 3) emit an event ([[25_Event_System]]), 4) record timestamp, 5) record actor, 6) record reason when applicable.
- Tasks must survive API/worker restarts (durable state in SQLite, leases/queues in Redis are reconstructible — see [[28_Reliability_Operations]]).

## Agent Lifecycle

States: `CREATED, INITIALIZING, READY, RUNNING, WAITING_TOOL, WAITING_APPROVAL, RECOVERING, COMPLETED, FAILED, TERMINATED`.

**Rules:**
- **Heartbeat/lease tracking:** agents renew a lease; a lease expiring marks the agent stale.
- **Orphan detection:** stale agents are reaped, never left zombie in `RUNNING` indefinitely.
- **Crash recovery:** on worker restart, orphaned runs are transitioned (`RECOVERING`) or failed cleanly with audit intact.

## Supervisor / Orchestrator Responsibilities

1. Understanding goals
2. Task decomposition
3. Dependency creation (explicit **DAG**)
4. Agent selection
5. Model selection ([[06_Feature_Multi_Model_Orchestration]])
6. Permission checks ([[27_Security_Permissions]])
7. Execution monitoring
8. Failure recovery ([[07_Feature_Error_Recovery]])
9. Cancellation
10. Result aggregation
11. Final completion

**Hard limits (configurable):** no dependency cycles, no infinite recursion, no unbounded task creation, no uncontrolled agent spawning, no uncontrolled concurrency. Enforce via [[28_Reliability_Operations]] resource limits.


---

---
title: Security & Permissions
type: spec
project: Agent System
status: locked
updated: 2026-09-06
up: "[[00_Index]]"
---

# Security & Permission System (v3.1 §12–§14)

> Centralized Permission Gate. **Never allow an agent to bypass the permission system.**

## Risk Levels & Policies

Risk: `LOW · MEDIUM · HIGH · CRITICAL`

Policies: `ALLOW_ONCE · ALLOW_SESSION · ALLOW_WORKSPACE · ALLOW_ALWAYS · DENY`

Each approval record must contain:

```json
{
  "approval_id": "approval_...",
  "task_id": "task_...",
  "agent_run_id": "run_...",
  "requested_action": "browser.submit_payment",
  "risk": "CRITICAL",
  "scope": "browser:transact",
  "requester": "BrowserAgent",
  "decision": "approved | denied | expired",
  "timestamp": "...",
  "expiration": "...",
  "reason": "..."
}
```

The v3.0 Approvals UI ([[16_Dashboard_UIUX]] §5.8) and CLI `agentctl approvals` operate on these records; approvals also expire (→ `approval.expired` event).

## Dangerous Operations — Default Deny

- Host filesystem access
- Credential extraction (SSH keys, browser password stores, .env)
- Arbitrary host shell
- System user modification
- Firewall changes
- Bootloader changes
- Security software modification
- Payment operations
- Credential transmission
- Arbitrary destructive host operations

**Autopilot is disabled by default** and has its own permission boundary (v3.1 §41 Phase 18 — implemented last).

## Security Model (Default Architecture)

- Localhost binding
- Explicit authentication/session secret
- Restricted CORS
- CSRF protection where applicable
- Secret redaction in all outputs
- Environment-variable protection
- Workspace isolation ([[08_Feature_Workspace_Templates]], [[19_Execution_Plan]] Phase 5)
- Docker isolation with resource limits
- Path traversal prevention
- Safe archive extraction
- File-size and output-size limits

## Secret Exposure — Never

Never expose `.env`, private keys, API keys, tokens, credentials, database URLs, browser profiles, or secret environment variables through:

- Reasoning/Decision & Execution Trace UI ([[05_Feature_Reasoning_Trace_Viewer]])
- Logs and audit records
- Recordings ([[09_Feature_Behavior_Recording]])
- Workspace templates ([[08_Feature_Workspace_Templates]])
- Model prompts
- Error messages

Redaction happens at the event/logging boundary, not at the UI layer only.

## Security Testing

Covered by the security test suite in [[29_Testing_Strategy]]: path traversal, secret leakage, sandbox escape, unauthorized tool access, permission bypass, unsafe archive extraction.


---

---
title: Reliability & Operations
type: spec
project: Agent System
status: locked
updated: 2026-09-06
up: "[[00_Index]]"
---

# Reliability & Operations (v3.1 §10, §11, §15, §20, §30–§32)

## Idempotency

Every retryable operation must be inherently idempotent or carry an idempotency key. Applies to: task creation · task execution · model calls · tool execution · scheduled jobs · artifact creation · replay · recovery · batching · recipe execution. Retries must never silently duplicate destructive operations.

## Crash Recovery

Assume every process can crash. Recovery must be tested for: API crash · worker crash · Redis crash · browser crash · Docker crash · LLM timeout · network outage · database lock · WebSocket disconnect · frontend reload · OS restart.

The system must: detect incomplete work · identify stale leases · recover safe operations · avoid duplicate destructive operations · preserve audit history · resume where safe · **fail clearly where resume is unsafe**.

Detailed runbook: `docs/implementation/RECOVERY.md` ([[30_Implementation_Docs]]).

## Storage Architecture

**SQLite is the authoritative local durable relational store.**

Configure: WAL · foreign keys · busy timeout · transactions · migrations · integrity checks.

**Redis is NOT the authoritative source of durable business state.** Redis is used for: queue · coordination · cache · ephemeral runtime state. If Redis disappears, durable task/session/audit data must remain recoverable from SQLite.

Use Alembic for migrations. Every schema change requires: migration + fresh-DB test + upgrade test + existing-DB test.

## Cost Accounting (Provider-Aware)

- Never assume all providers expose identical token metrics. Support: input tokens, output tokens, cached tokens (where available), estimated tokens, unknown usage.
- **Unknown cost must not crash execution** — mark estimated/unknown explicitly.
- Budget levels: 50% / 75% / 90% / 100%.
- Budget scopes: per-task · per-session · daily · per-provider.

Extends [[10_Feature_Cost_Optimizer]]; ModelCall records defined in [[24_Canonical_Domain_Model]].

## Resource Limits (Configurable)

| Limit | Purpose |
| --- | --- |
| `max_concurrent_agents` / `max_concurrent_tasks` | Concurrency control |
| `max_workspace_size` / `max_file_size` / `max_output_size` / `max_log_size` | Storage growth |
| `max_browser_sessions` | Browser pool |
| `max_container_cpu` / `max_container_memory` | Sandbox containment |
| `max_execution_time` | Runaway tasks |
| `max_task_tokens` / `max_task_cost` | Per-task spend |
| `max_retries` | Retry loops |

When limits are exceeded: **stop safely · persist reason · emit event · record audit entry.**

## Degraded Modes

`FULL · DEGRADED · OFFLINE · READ_ONLY · RECOVERY`

| Failure | Behavior |
| --- | --- |
| Redis unavailable | Durable state remains inspectable from SQLite |
| LLM unavailable | Tasks remain queued or fail clearly |
| Browser unavailable | Browser tasks become recoverable failures |
| Vector DB unavailable | Core execution continues where vector memory is not required |

## Memory Layers

Separate: **SYSTEM MEMORY · USER MEMORY · TASK MEMORY · WORKSPACE MEMORY.** Do not store secrets in human-readable memory.

- Obsidian = human-readable layer ([[20_Deployment]] `VAULT_PATH`).
- LanceDB = vector retrieval layer.
- Memory writes must be attributable to: source · timestamp · task/session · agent · confidence (where applicable).
- Secret filtering enforced per [[27_Security_Permissions]].

## Observability

- `/health` and `/ready` endpoints.
- Structured, machine-readable logging where appropriate.
- Every important operation traceable via: `request_id` · `session_id` · `task_id` · `agent_run_id`.
- **Never leak secrets into logs.**


---

---
title: Testing Strategy
type: spec
project: Agent System
status: locked
updated: 2026-09-06
up: "[[00_Index]]"
---

# Testing Strategy & Chaos Engineering (v3.1 §39, §40, §44)

## Test Pyramid

| Layer | Scope |
| --- | --- |
| **Unit** | Core domain/services |
| **Integration** | API+DB · API+Redis · worker+DB · worker+Redis · agent+sandbox · model router · scheduler · memory |
| **Contract** | API schemas (`/api/v1`) |
| **Security** | Path traversal · secret leakage · sandbox escape · unauthorized tool access · permission bypass · unsafe archive extraction |
| **Recovery** | Kill workers mid-execution · restart services · disconnect WebSockets · simulate Redis failure · simulate LLM timeout |
| **Replay** | Verify inspect/simulate/re-execute boundaries ([[09_Feature_Behavior_Recording]]) |
| **Migration** | Fresh DB · existing DB upgrade |
| **Frontend** | Critical user journeys ([[16_Dashboard_UIUX]]) |
| **CLI** | JSON schema and exit codes ([[17_CLI_Specification]]) |

## Chaos Testing (before declaring production-ready)

Intentionally test and require predictable failure:

- Worker crash · API restart · Redis restart · browser crash · container crash
- Network timeout · LLM timeout · database lock
- WebSocket disconnect
- Task cancellation · approval expiration
- Duplicate event · duplicate task submission

## End-to-End Acceptance Test

User goal: *"Create a workspace, inspect the repository, identify failing tests, fix the failures, run QA, summarize the changes, and produce a report."*

Expected lifecycle (verify after clean installation/restart):

```
Session created → Task created → Supervisor plans → Workspace created
→ CodeAgent starts → Model selected → Model usage recorded
→ Tool calls recorded → Decision/execution events emitted
→ Code changes made → Failure detected if present → Recovery executed
→ QA runs → Results recorded → Diff generated
→ Approval requested where required → Approval granted
→ Changes committed → Artifact generated → Audit trail completed
→ Memory updated → Insight generated where applicable
→ Final response returned
```

## Phase Gate Relation

A phase cannot close ([[23_Engineering_Contract]]) with failing critical tests, broken migrations, or security violations. Test status is recorded per phase in [[21_Progress_Log]] and [[30_Implementation_Docs]].


---

---
title: Implementation Docs Index
type: moc
project: Agent System
status: active
updated: 2026-09-06
up: "[[00_Index]]"
---

# Implementation Documentation Contract (v3.1 §36–§37, §46)

> When implementation begins, these living documents must exist in the repository at `docs/implementation/` and evolve with the code. This note is the master index and status tracker; vault notes hold the specs, repo docs hold the as-built truth.

## Required Documents

| Repo path | Content | Owner phase |
| --- | --- | --- |
| `REPOSITORY_INVENTORY.md` | Full repo audit: modules, entry points, dependency graph, DB schema, routes, tests, tech debt, dangerous/broken functionality | Phase 0 ([[19_Execution_Plan]]) |
| `ARCHITECTURE.md` | As-built architecture & dependency layers ([[24_Canonical_Domain_Model]]) | continuous |
| `API.md` | As-built `/api/v1` contract ([[18_API_Reference]]) | Phase 3+ |
| `EVENTS.md` | As-built event catalog & envelope ([[25_Event_System]]) | Phase 2+ |
| `RECOVERY.md` | Crash-recovery runbook ([[28_Reliability_Operations]]) | Phase 4+ |
| `SECURITY.md` | As-built security model ([[27_Security_Permissions]]) | Phase 3+ |
| `THREAT_MODEL.md` | Threats, boundaries, mitigations | Phase 3+ |
| `DECISIONS.md` | Engineering decision log (mirrors [[22_Decision_Log]]) | continuous |
| `STATUS.md` | Phase/status snapshot | continuous |
| `OPERATIONS.md` | Startup, backup, upgrade runbook ([[20_Deployment]]) | Phase 1+ |
| `TROUBLESHOOTING.md` | Known failure modes & fixes | continuous |
| `AGENT_STATE.md` | Current phase · completed phases · current task · active blockers · tests last executed · known failures · remaining work · known limitations — enables another autonomous agent to resume safely | continuous |
| `FINAL_REPORT.md` | Architecture summary · implemented features · changed files · schema · API/event/security/permission models · startup & test commands · results · deployment · known limitations · required credentials · recovery procedures · future work | end |

## Completion Labels (FINAL_REPORT)

Clearly distinguish: **IMPLEMENTED · PARTIALLY IMPLEMENTED · CONFIGURATION REQUIRED · KNOWN LIMITATION · BLOCKED**. Never claim 100% completion if anything remains incomplete.

## Final System Validation Checklist

Fresh install ✓ · migrations ✓ · API starts ✓ · worker starts ✓ · Redis ✓ · frontend builds ✓ · CLI works ✓ · auth ✓ · permissions ✓ · tasks persist ✓ · tasks survive restart ✓ · agent lifecycle ✓ · event system ✓ · WS reconnect ✓ · SSE reconnect ✓ · sandbox ✓ · workspace isolation ✓ · browser agent ✓ · document generation ✓ · memory ✓ · scheduler ✓ · model router ✓ · cost tracking ✓ · error recovery ✓ · QA ✓ · templates ✓ · replay safety ✓ · batching ✓ · recipes ✓ · personality ✓ · insights ✓ · audit ✓ · autopilot gated ✓ · no secret leaks ✓ · security tests ✓ · chaos tests ✓ · migration tests ✓ · UI smoke ✓ · CLI tests ✓ · docs match implementation ✓


---

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

