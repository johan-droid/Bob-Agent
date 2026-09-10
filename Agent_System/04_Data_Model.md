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
