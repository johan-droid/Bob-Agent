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
