# TROUBLESHOOTING (implementation)

> **Updated:** 2026-09-16 · Symptom → cause → action. Commands run from `agent-system/backend/`.

## First moves

```bash
uv run agentctl doctor          # environment probe
curl -s localhost:8000/api/v1/ready | jq    # api + db readiness
uv run alembic current                      # schema revision
uv run pytest -q                            # full suite
```

## Capability and permission problems

### "awaiting approval <id> for: shell ..." / task fails with an approval id

Expected. The capability is gated and no live grant exists.

1. `GET /api/v1/approvals` → find the PENDING record.
2. `POST /api/v1/approvals/{id}/decision {"approve": true, "policy": "ALLOW_ONCE"}`.
3. `POST /api/v1/tasks/{id}/retry` (FAILED → QUEUED is explicitly allowed).

If the approval never becomes visible, you are running more than one database:
`DATABASE_URL` must be identical for the API and the worker — the gate is durable, so a
mismatch looks exactly like "my approval did nothing".

### "permission_denied ... default-deny scope"

The capability targets a `DANGEROUS_SCOPES` entry or is `destructive` tier
(`git_reset_hard`, `git_clean`, `git_push_force`). This is intentional and cannot be
approved. Use a non-destructive alternative (`git_restore`, `git_checkout`) or do the
operation manually outside the agent.

### "invalid_arguments for 'X': ..."

The model's call failed schema validation and never reached the handler. The message lists
the reason (`missing required argument`, `unknown argument (accepted: ...)`, `expected
integer, got str`, `expected one of [...]`). This is a model-side fix:
`capabilities_list` shows the accepted schema; re-prompt with it.

### "unknown tool 'foo'. Available: ..."

The model invented a capability. The available list is in the same result. Note that
optional integrations are **absent** from the registry when unconfigured — if
`openconnector_execute` is missing, `OPENCONNECTOR_BASE_URL` is unset.

## Execution and sandbox problems

### "sandbox exec failed" / "sandbox unavailable"

`tools_shell_mode=sandbox` needs a Docker daemon. Either start Docker, or set
`HEROKU_JAIL=true` / `tools_shell_mode=jail` (containment, not isolation), or
`tools_shell_mode=off` to disable process execution entirely.

### `sandbox image 'agent-system/qa-sandbox:latest' is not available`

The QA sandbox image is built by this repository and published to no registry, so a fresh
checkout does not have it. Build it once:

```bash
make qa-sandbox-image     # docker build -t agent-system/qa-sandbox:latest -f backend/docker/qa-sandbox.Dockerfile backend/docker
```

The build is idempotent and Docker caches the layers, so re-running is cheap. CI builds it
before pytest, which is why the Docker-gated QA test executes there rather than skipping.
Programmatically, `DockerSandbox.ensure_image(DockerSandbox.QA_IMAGE)` builds on demand.
Note the deliberate distinction: only locally-built images (`DockerSandbox.LOCAL_IMAGES`)
produce this hint. A start failure for a registry image such as `python:3.12-slim` stays a
generic sandbox error, because the fix there is not "run the build".

### "process execution is disabled (tools_shell_mode=off)"

Working as configured. This affects `shell`, `git_*`, `run_tests`/`run_linter`/
`run_typecheck`/`run_formatter`. File capabilities still work.

### "path outside allowed roots: /tmp/..."

Capabilities are jailed to `workspaces/`, `outputs/`, the process CWD, and `TOOLS_FS_ROOTS`.
Add the directory to `TOOLS_FS_ROOTS` (comma-separated) if it is genuinely required, or
place the work inside `workspaces/`.

### "refusing secret path: ..."

`is_secret_path` matched. This is deliberate and not configurable per path; do not work
around it from a capability.

## Event and audit problems

### `UnknownEventTypeError: unknown event type 'x.y'`

A producer emitted a name outside the taxonomy. Add it to `domain/events.py` (canonical) or
register it with `register_event_extensions(owner, (...))` — never emit an ad-hoc literal.
The error message lists the accepted extension owners.

### The UI shows no `tool.called` events

Renamed: the canonical events are `tool.started`, `tool.completed`, `tool.failed`. The old
names are rejected on purpose.

### Events stop appearing / sequence errors

Two `EventBus` instances with stale sequence counters used to collide. Pass the caller's bus
through the execution context (the worker and orchestrator do). `emit` re-reads
`max(sequence)` once per process, so a fresh process is always safe.

## Budget problems

### "BudgetExceededError: daily budget exhausted"

Spend is derived from the `model_calls` table, so it is real and survives restarts.

```bash
uv run python -c "
from agent_system.config import get_settings
from agent_system.infra.db import make_engine, make_session_factory
from agent_system.services.budget import BudgetLedger
s=get_settings(); f=make_session_factory(make_engine(s.database_url))
print(BudgetLedger(f, s).snapshot()['scopes'])
"
```

Raise `DAILY_BUDGET_USD` / `MAX_TASK_COST_USD` / `MAX_TASK_TOKENS`, or delete rows only if
you accept losing the audit trail. Note the in-memory `BudgetMonitor` is legacy — alerts are
computed from persisted spend now, so thresholds do not double-fire after a restart.

## Planning problems

### 400 "invalid plan: plan task 'x' requires unavailable capabilities: ..."

The planner proposed capabilities the registry does not have (an optional integration is
unconfigured, or a plugin/capability was removed). Configure the integration or adjust the
goal. The session is marked `PLANNING_FAILED` rather than executing a bad DAG.

### 409 "session already has tasks; planning is one-shot"

Delete the tasks (or the session) before re-planning. This prevents silently duplicating a
DAG.

### A goal planned as `generic`

No intent pattern matched, or the goal was not plannable. Check
`documentations/` for the product intent. The plan response includes `intent`, `strategy` and
`warnings` — `strategy` is always `deterministic_keyword_v1` today (there is no LLM planner).

## Testing problems

### Tests fail with "table ... has no column ..."

Your local database is behind the models. `uv run alembic upgrade head` (the test suite does
this automatically per run; a manually started API process does not).

### Tests fail only in a different order

Settings are cached per process. `tests/conftest.py` clears the cache around every test; if
you add a fixture that mutates env vars, rely on that autouse fixture rather than importing
`get_settings` at module scope.

### One failure: `test_real_docker_sandbox_runs_untrusted_test`

Requires **both** a Docker daemon and the QA sandbox image (see above). The test now builds
the image on demand when the source tree is present, and skips with an explicit reason
(`docker daemon unavailable` / `qa sandbox image unavailable`) when it genuinely cannot run.
With the image built, the suite is fully green — this is no longer a standing failure.

## Web dashboard

### Dashboard shows stale/empty data

The UI proxies `/api/v1` to the backend; start it with `make dev` or `make start`. A build
is required for `next start` (`make web-build`). The smoke test
(`web/smoke-test.mjs`) boots both and verifies the proxy delivers real data.

### `npx tsc --noEmit` fails in CI

CI runs typecheck + build + smoke as separate steps — read the failing step name to know
whether it is types, the build, or the runtime proxy.
