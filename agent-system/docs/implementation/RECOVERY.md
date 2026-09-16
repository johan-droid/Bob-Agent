# RECOVERY (implementation)

> **Updated:** 2026-09-16 · SQLite is the durable record; the queue is reconstructible.

## Principles

1. Queue and in-process state are disposable; the database is not.
2. Every crash path is detected by a lease, not by a timeout guess.
3. Recovery is visible: `recovery.started` / `recovery.completed` / `recovery.failed`.
4. Nothing destructive is retried automatically.

## Lease and heartbeat model

| field | meaning |
| --- | --- |
| `agent_leases.heartbeat_at` | last heartbeat from the running worker |
| `agent_leases.lease_expires_at` | `now + LEASE_TTL_SECONDS` (worker 60s, in-process 30s) |
| `agent_runs.state` | RUNNING while the lease is live |
| `tasks.attempt` | incremented **only** on entry to RUNNING (never on requeue) |

The worker refreshes its lease every 15s from a daemon thread. If the process dies, the
lease expires and the reaper owns the run.

## Failure matrix

| failure | detection | behaviour |
| --- | --- | --- |
| worker killed mid-task | stale lease | `recovery.started`; RUNNING → RECOVERING → QUEUED if `attempt < 3`; `recovery.completed` with the action |
| retries exhausted | stale lease, `attempt >= 3` | RUNNING → FAILED, `last_error="lease expired; retries exhausted"`, `recovery.failed` |
| RQ duplicate delivery | `task.state != QUEUED` on entry | returns `{"skipped": true}`; not an error |
| API/worker restart | process start | all state re-read from SQLite; `EventBus` re-reads `max(sequence)`; approvals and derived budgets persist |
| duplicate `event_id` | `session.get(EventRow, id)` | the stored row is returned; no sequence burned, no duplicate fanout |
| provider outage | consecutive `model.failed` ≥ threshold | circuit opens (fast-fail, no network), half-open probe after cooldown, closes on success; `model.circuit_opened`/`closed` |
| model call fails | exception inside `invoke` | the loop returns `stopped="error"`; the task fails honestly with the provider error |
| capability crash | handler exception | structured `capability_crash` result returned to the model; `tool.failed` emitted; the run continues |
| invalid arguments | schema validation | `invalid_arguments` returned to the model **before** any handler runs; `tool.failed` |
| approval required | gate returns WAIT | `agent.waiting_approval`; the task fails honestly carrying the approval id; after approval the task can be retried (`FAILED → QUEUED` is explicitly allowed) |
| approval expired | TTL sweep / check | `approval.expired`; the request denies |
| context overflow | token estimate vs budget | low-value results compacted, important ones retained, `context.compacted` |
| planning failure | `PlanningError` / invalid DAG | session marked `PLANNING_FAILED`, HTTP 400, no partial DAG executed |
| cancellation | user request | QUEUED → CANCELLED immediately; RUNNING records a cancel request checked before the handler |
| database unavailable | connection error | the API reports `degraded` on `/ready`; the worker's heartbeat thread exits and the lease reaper recovers the run |

## Retry policy

Retries happen only for lease expiry. A FAILED task is retried only when a human or the API
explicitly asks (`POST /api/v1/tasks/{id}/retry`), and the transition table rejects the
request for non-retryable states. Destructive, permission and validation failures are never
retried automatically.

## Restart-safety evidence

| property | test |
| --- | --- |
| state survives a fresh engine over the same file | `tests/recovery/test_persistence_restart.py` |
| event dedupe across open/close/reopen | `tests/recovery/test_phase19_hardening.py` |
| lease expires and the task is requeued, `attempt` not double-counted | `tests/integration/test_worker.py`, `scripts/e2e_crash_recovery_check.py` |
| approval decisions survive restart | `TestApiApprovalUnblocksCapability::test_decision_survives_restart` |
| budget spend survives restart and still blocks | `TestBudgetSurvivesRestart` |
| planned DAG executes in dependency order after re-planning | `TestEndToEndExecutionPath` |

## Manual recovery

```bash
cd backend
uv run alembic upgrade head        # schema is current
uv run pytest -q tests/recovery    # recovery suite
uv run python -m agent_system.worker   # lease reaper runs on worker startup paths
```

`Orchestrator.recover_orphans(factory)` can be invoked directly to reap stale leases; it is
idempotent for already-recovered tasks.
