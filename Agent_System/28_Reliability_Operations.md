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
