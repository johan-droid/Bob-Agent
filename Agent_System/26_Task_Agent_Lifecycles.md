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
