# EVENTS (implementation)

> **Updated:** 2026-09-16 · `domain/events.py` is the single source of truth.
> The bus rejects unknown event types at emit time.

## Envelope

Every event carries the canonical envelope (`domain/events.Event`):

```json
{
  "event_id": "evt_01J...",
  "schema_version": 1,
  "session_id": "ses_...",
  "task_id": "task_...",
  "agent_run_id": "run_...",
  "sequence": 123,
  "timestamp": "2026-09-16T12:00:00Z",
  "type": "tool.completed",
  "actor": "generic",
  "payload": {},
  "visibility": "user",
  "sensitivity": "normal"
}
```

`sequence` is assigned by `EventBus.emit` (globally monotonic, unique index). `visibility`
is `user` or `internal`; `sensitivity` is `normal` or `sensitive`.

## Guarantees

| property | how |
| --- | --- |
| ordering | monotonic `sequence`, per-session ordering guaranteed by the index `ix_events_session_sequence` |
| deduplication | `emit` returns the stored row for a repeated `event_id`; no new sequence, no duplicate fanout |
| replay | `EventBus.replay_after(session, after_sequence, event_type, session_id)` → `GET /api/v1/events?after_sequence=` |
| audit | append-only `events` table |
| redaction | `redact_payload` replaces values under secret-looking keys at emit time |
| validation | `validate_event_type` raises `UnknownEventTypeError` for anything outside the taxonomy |
| subscriber isolation | a throwing subscriber never breaks `emit` |

## Canonical catalog

Producers in parentheses.

| domain | events |
| --- | --- |
| session | `session.created`, `session.updated`, `session.completed`, `session.deleted` |
| task | `task.created`, `task.queued`, `task.started`, `task.completed`, `task.failed`, `task.cancelled`, `task.recovering`, `task.blocked_approval` |
| agent | `agent.created`, `agent.started`, `agent.waiting_tool`, `agent.waiting_approval`, `agent.completed`, `agent.failed`, `agent.terminated`, `agent.fallback_applied` |
| model | `model.requested`, `model.completed`, `model.failed`, `model.token`, `model.circuit_opened`, `model.circuit_closed` |
| tool | `tool.started`, `tool.completed`, `tool.failed` |
| approval | `approval.requested`, `approval.approved`, `approval.denied`, `approval.expired` |
| workspace | `workspace.created`, `workspace.modified`, `workspace.destroyed`, `workspace.restored` |
| artifact | `artifact.created`, `artifact.deleted` |
| qa | `qa.started`, `qa.completed`, `qa.failed` |
| recovery | `recovery.started`, `recovery.completed`, `recovery.failed` |
| recipe | `recipe.started`, `recipe.completed`, `recipe.failed` |
| cost | `cost.recorded`, `cost.alert` |
| insight | `insight.generated` |
| context | `context.compacted` |

**Producers:** API routers (session/workspace/approval/artifact), `Supervisor` +
`Orchestrator` + `worker.py` (task/agent lifecycle), `ModelRouter` (model/cost),
`ReAct loop` (tool/context/agent.waiting_approval), `PermissionGate` (approval decisions),
`agents/registry.py` (agent.fallback_applied), feature services (qa/recovery/recipe/insight).

## Registered extensions

Subsystems with their own operational events declare them once:

```python
register_event_extensions("skills", ("skill.created", "skill.updated", "skill.deleted"))
register_event_extensions("a2a", ("a2a.delegated", "a2a.result", "a2a.failed"))
register_event_extensions("backup", ("backup.completed", "backup.failed"))
```

Rules: an extension cannot collide with the canonical catalog or another owner;
`event_extensions()` and `known_event_types()` expose the full accepted set.

## Retired names

`tool.called` and `tool.result` were emitted by earlier revisions and are **not** accepted
(no aliasing): `tool.started` / `tool.completed` / `tool.failed` replaced them. A test
asserts the retired names are rejected, so a stale producer fails loudly instead of writing
an event no consumer understands.

## Payload conventions

| event | payload keys |
| --- | --- |
| `tool.started` | `tool`, `capability_risk`, `protocol` (`bob_fenced` \| `provider_native`) |
| `tool.completed` / `tool.failed` | `tool`, `ok`, `capability_risk`, reason on failure |
| `agent.waiting_approval` | `tool`, `approval_id`, `denied` |
| `model.token` | `model_id`, `delta` |
| `context.compacted` | `dropped_count`, `kept_count`, `estimated_tokens_saved`, `retained_important` |
| `cost.alert` | `scope`, `level_pct`, `spent_usd`, `limit_usd`, `model_call_id`, `source` |
| `agent.fallback_applied` | `requested_type`, `resolved_agent`, `fallback_reason` |
| `approval.requested` | `approval_id`, `scope`, `risk`, `outcome` |

## Adding an event

1. Operational and product-visible → add it to the relevant tuple in `domain/events.py`.
2. Subsystem-private → declare it in the subsystem's own module with
   `register_event_extensions(owner, (...))`.
3. Never emit a string literal that is not one of the above; the bus raises.
