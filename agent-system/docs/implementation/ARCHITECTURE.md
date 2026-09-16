# ARCHITECTURE (implementation)

> **Updated:** 2026-09-16 · Describes what the code does today. Aspirational product
> requirements live in `documentations/`; see `ARCHITECTURE_RECONCILIATION.md` for the
> before/after and the full inventory.

## Layering

```text
API          backend/src/agent_system/api/          FastAPI routers
Services     backend/src/agent_system/services/     application logic
Domain       backend/src/agent_system/domain/       events, lifecycles, ids
Infrastructure backend/src/agent_system/infra/      db, event bus, ORM models, telemetry
```

Dependency direction is enforced by convention and review: API → services → domain →
infrastructure. Capabilities do not issue raw SQL; they call services or use the
`ToolContext` factory through `session_scope`.

## Request and execution spine

```text
POST /api/v1/sessions/{id}/plan
  services/planner.py      Planner.plan(goal)          intent, DAG, capabilities, risk
  services/orchestrator.py Supervisor.validate_plan    cycles, deps, limits, capabilities
                           Supervisor.apply_plan      persist tasks in topological order
                           Supervisor.plan            PENDING -> QUEUED for ready work

POST /api/v1/tasks/{id}/retry | GET /api/v1/... (existing contracts, unchanged)

worker.py (RQ) or services/cloud.drive_session (in-process)
  services/orchestrator.py Orchestrator.run_ready_tasks / _run_task
                           AgentRun + AgentLease + agent.started
  agents/registry.py       resolve(agent_type) -> definition + handler
  agents/react_agent.py    llm_react_handler -> model calls + capability loop
  services/agent_loop.py   ReAct: parse ToolCall -> execute the capability -> feed back
```

## Capability pipeline

```text
model message
  services/tools/protocol.py   provider-native tool_calls  OR  ```tool:name fence
                               both produce services.tools.protocol.ToolCall
  services/tools/execution.py  validate arguments (schemas.py)
                               authorize (permissions.require_capability)
                               run the handler
  services/permissions.py      classify_risk -> PermissionGate.authorize
                               ALLOW | DENY | WAIT   (durable, DB-backed)
```

`services/tools/` layout: `registry.py`, `schemas.py`, `execution.py`, `protocol.py`,
`paths.py`, `optional.py`, `builtin/` (10 groups), `plugins/`.

An individual capability declares a risk tier and a scope; it never evaluates permissions.
`plan_permission()` is the single place the documented decision (`allowed` / `approval` /
`deny`) is derived.

## Permission system

```text
capability request
  → risk classification        services.permissions.classify_risk
  → policy evaluation          PermissionGate.check
  → existing approval lookup   durable APPROVED grant, unexpired, policy-scoped
  → approval request if needed PermissionGate.request → PENDING
  → allow / deny / wait        PermissionGate.authorize
  → tool executor
```

Records live in the `approvals` table when the gate is constructed with a session factory
(the API, worker and capabilities all do). ALLOW_ONCE consumption and expiry are persisted,
so decisions hold across processes and restarts. `DANGEROUS_SCOPES` and the `destructive`
tier are default-deny and cannot be granted by anyone.

## Event system

`domain/events.py` owns the canonical catalog plus an explicit extension mechanism for
subsystem-owned operational events. `infra/event_bus.py` validates the type, redacts secret
keys, assigns a globally monotonic sequence, persists to `events`, and fans out to
subscribers. One bus; no subsystem creates its own.

## Persistence

SQLite (WAL) is authoritative: `sessions`, `tasks`, `agent_runs`, `agent_leases`,
`approvals`, `events`, `model_calls`, `tool_calls`, `artifacts`, `memory_notes`, plus the
v3.0 feature tables. Redis (when present) carries only queue/job state, which is
reconstructible. Budget spend is derived from `model_calls` (`services/budget.py`).

## Agents and skills

- **Agents** are policy declarations (`agents/definitions.py`) resolved by
  `agents/registry.py:resolve()`; several agents share the one ReAct engine.
- **Skills** are procedural knowledge (`services/skills.py`, `skills/<name>/SKILL.md`)
  injected into prompts. A skill cannot execute anything and bypasses no permission check.
- **Memory** is persistent information (`services/memory.py`). The distinction
  skill (procedural) / tool (executable) / memory (information) is maintained.

## Sandbox

One execution seam: `services/tools/builtin/_exec.py:run_workspace_command`.

```text
capability -> workspace (jailed, allowed roots) -> DockerSandbox | SubprocessJail | local | off
```

No capability receives a Docker socket. Filesystem reachability is decided in
`services/tools/paths.py` (symlinks resolved before containment, secret paths always
refused).

## Known architectural limits

- The Planner is deterministic (`deterministic_keyword_v1`); no LLM planner exists.
- The `Supervisor` schedules synchronously in-process; concurrency limits are enforced at
  task creation (`MAX_TASKS_PER_SESSION`, `MAX_AGENT_SPAWN_PER_TASK`), not by a distributed
  scheduler.
- Browser capabilities require the optional Playwright dependency.
