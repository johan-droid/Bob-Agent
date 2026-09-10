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
