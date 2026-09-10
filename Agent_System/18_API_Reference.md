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
