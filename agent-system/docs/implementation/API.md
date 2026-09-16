# API (implementation)

> **Updated:** 2026-09-16 · Base path `/api/v1` · All mutating endpoints require
> `Authorization: Bearer <token>`. Machine-readable `--json` equivalents exist in the CLI
> against these same contracts.

## Auth

| method | path | notes |
| --- | --- | --- |
| POST | `/auth/token` | exchanges the bootstrap secret for a session token (403 on mismatch) |
| GET | `/health` | liveness, no auth |
| GET | `/ready` | readiness with a `checks` block (`ok` \| `degraded`) |

## Sessions

| method | path | notes |
| --- | --- | --- |
| POST | `/sessions` | `{goal}` → 201 `{id, goal, status}`; emits `session.created` |
| GET | `/sessions` | `limit`, `offset` |
| GET | `/sessions/{id}` | 404 when unknown |
| PATCH | `/sessions/{id}` | updates the goal; emits `session.updated` |
| **POST** | **`/sessions/{id}/plan`** | **plans the goal into a task DAG (see below)** |
| DELETE | `/sessions/{id}` | deletes tasks then the session; emits `session.deleted` |

### `POST /sessions/{session_id}/plan`

Runs the Planner and the Supervisor and leaves the work QUEUED. Execution belongs to the
Orchestrator/worker and is not triggered here.

```json
201 {
  "session_id": "ses_...",
  "intent": "documents",
  "risk": "MEDIUM",
  "strategy": "deterministic_keyword_v1",
  "warnings": [],
  "tasks": [
    {"key": "gather", "title": "...", "task_type": "research_light", "agent_type": "llm",
     "depends_on": [], "required_capabilities": ["research_search"], "expected_outputs": ["findings"], "risk": "LOW"},
    {"key": "produce", "title": "...", "task_type": "documents", "agent_type": "llm",
     "depends_on": ["gather"], "required_capabilities": ["document_create"], "expected_outputs": ["artifact"], "risk": "MEDIUM"}
  ]
}
```

| response | meaning |
| --- | --- |
| 201 | plan persisted; tasks created in topological order |
| 400 | unplannable goal, or an invalid plan (cycle, unknown dependency, unavailable capability). The session is marked `PLANNING_FAILED` |
| 404 | unknown session |
| 409 | the session already has tasks — planning is one-shot, so a DAG is never silently duplicated |

Dependency-free tasks become QUEUED immediately; dependent tasks stay PENDING until their
dependency succeeds (the Supervisor schedules; only the Orchestrator runs).

## Tasks

| method | path | notes |
| --- | --- | --- |
| POST | `/tasks` | create with `depends_on` and optional `idempotency_key` |
| GET | `/tasks` | filters `session_id`, `state`, `limit ≤ 200` |
| GET | `/tasks/{id}` | single task |
| POST | `/tasks/{id}/transition` | validated against the transition table; emits the matching event |
| POST | `/tasks/{id}/retry` | `FAILED → QUEUED` only |

## Approvals

| method | path | notes |
| --- | --- | --- |
| POST | `/approvals` | request; 202 with a PENDING record (or DENIED for a default-deny scope); emits `approval.requested` |
| GET | `/approvals` | `pending_only` (default true) |
| POST | `/approvals/{id}/decision` | `{approve, policy, reason}`; emits `approval.approved` / `approval.denied` / `approval.expired` |
| POST | `/approvals/sweep` | expires overdue PENDING records |

These endpoints operate on the **same durable store** the capability layer uses, so a
decision here unblocks a waiting capability (including in another process).

## Events and realtime

| method | path | notes |
| --- | --- | --- |
| GET | `/events` | `after_sequence` for resume; canonical envelopes |
| WS/SSE | `/realtime...` | authenticated stream, heartbeat, resume-from-sequence |

## Workspaces, artifacts, outputs

`POST/GET/DELETE /workspaces`, `/workspaces/{id}/exec`, `/workspaces/{id}/files`,
`/workspaces/{id}/clone`, `/workspaces/{id}/restore`, `/workspaces/{id}/templates/*`,
`/artifacts`, `/outputs`, `/recordings`, `/recordings/{id}/replay`, `/vault`, `/templates`
— unchanged by the reconciliation; `workspace.*` and `artifact.*` events are canonical.

## Features

`/model-calls`, `/qa-reports`, `/recipes`, `/batch`, `/insights`, `/personality`,
`/schedule`, `/autopilot`, `/a2a`, `/skills`, `/settings`, `/soul`, `/providers`, `/tools`.

- `/skills` — CRUD + import; emits `skill.*` (registered extension).
- `/tools` — lists real capabilities from the registry (name, group, risk, permission).
- `/autopilot` — off by default, per-action fresh approvals, sticky kill switch (its own
  endpoint contract is unchanged).

## Error shape

FastAPI defaults: `{"detail": ...}` for 4xx/5xx. Validation failures return the structured
`{"error": "invalid_arguments", "tool": ..., "detail": [...], "hint": ...}` payload when
they originate from a capability invoked through the loop (returned *to the model*, not to
the API caller).

## Compatibility notes

- All pre-existing endpoints and response fields are preserved.
- Capability names changed where the library restructured (`tool.called`/`tool.result`
  events were replaced by `tool.started`/`tool.completed`/`tool.failed`).
- `web_fetch` remains available as a `research_fetch` alias.
