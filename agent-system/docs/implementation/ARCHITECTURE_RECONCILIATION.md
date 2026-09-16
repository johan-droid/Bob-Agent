# Architecture Reconciliation — Bob-Agent

> **Date:** 2026-09-16 · **Scope:** architecture coherence, permission unification, event
> taxonomy, capability library, planning separation, cost durability, CI.
> **Evidence:** every claim below is backed by a test or a command recorded in §I.

This document is the required output of the deep architecture reconciliation. It records
what the system was, what diverged, what changed, and what is still not true.

---

## A. Before — the architecture as it actually was

`agent-system/backend/src/agent_system/` was layered as documented (`api → services →
domain → infra`), with one execution spine:

```text
API (/api/v1)
  → services/orchestrator.py     Supervisor (decompose+persist DAG) + Orchestrator (run)
  → infra/db.py, infra/event_bus.py, infra/models.py

Worker (RQ, worker.py)
  → agents/registry.py           handler lookup with a silent default
  → agents/react_agent.py        llm_react_handler
  → services/agent_loop.py       ReAct loop; parsed fenced ```tool: blocks
  → services/tools.py            single 831-line module, 9 tools, inline approval
  → services/permissions.py      PermissionGate (in-memory)  ← API used this
  → infra/models.py Approval     DB helper approvals          ← tools used this
```

Agent-visible surface: 9 tools (`shell`, `file_read`, `file_write`, `file_list`,
`web_fetch`, `memory_recall`, `memory_remember`, `tasks_inspect`, plus conditional
`openconnector_*`/`mcp_*`). Tool calls were parsed only from text.

Tests: 517 passed, 13 failed (mixed cache/order failures masking a real integration bug).

---

## B. Problems found

| # | Divergence | Evidence | Status now |
| --- | --- | --- | --- |
| B1 | **Two permission systems.** `PermissionGate` (in-memory, used by `/api/v1/approvals`) and `_db_approval_ok`/`_db_request_approval` (DB rows, used by tools). An approval granted in the UI **could never unblock the tool that requested it**, and the gate's records were lost on restart. | `services/permissions.py`, `services/tools.py:181-240` | **FIXED** — one DB-backed gate |
| B2 | **Risk taxonomy split.** Canonical `LOW/MEDIUM/HIGH/CRITICAL` existed in the gate; tools used lowercase `read/write/execute` with no mapping. | `services/permissions.py` vs `services/tools.py` | **FIXED** — `CapabilityRisk` → `Risk` map |
| B3 | **No individual-tool policy.** Approval logic was duplicated inside `_shell`, `_openconnector_execute`, `_mcp_call`, `_require_plugin_approval` — four copies, each able to drift. | `services/tools.py`, `services/tool_plugins.py:290` | **FIXED** — one `execution.py` path |
| B4 | **Fabricated event names.** `tool.called`, `tool.result`, `model.token`, `context.compacted`, `cost.alert`, `model.circuit_*`, `session.updated/deleted`, `workspace.restored`, `skill.*`, `a2a.*`, `backup.*` were emitted but absent from the canonical catalog; nothing validated them. | `domain/events.py` vs producers | **FIXED** — canonical catalog + registered extensions + emit-time validation |
| B5 | **`agent.waiting_tool` / `agent.waiting_approval` declared but never emitted.** Agents paused for tools/approvals with no lifecycle event. | catalog vs `agent_loop.py` | **FIXED** — both emitted |
| B6 | **No runtime argument validation.** Model-supplied arguments went straight into handlers; malformed JSON leaked in as `{"_raw": ...}`. | `services/agent_loop.py` | **FIXED** — validated before the handler |
| B7 | **Capability library did not exist.** One 831-line module with 9 tools; no coding/git/browser/document groups; coding depended entirely on `shell`. | `services/tools.py` | **FIXED** — 61 capabilities in 10 groups |
| B8 | **Browser automation and web research were the same thing** (`web_fetch` was the only web capability). | `services/tools.py:_web_fetch` | **FIXED** — `browser_*` vs `research_*` |
| B9 | **Planner did not exist.** `Supervisor.plan()` only queued already-created tasks; the generic ReAct agent was the de-facto planner. No intent, dependency derivation, capability requirements, expected outputs or risk classification. | `services/orchestrator.py` | **FIXED** — `services/planner.py` + `Supervisor.validate_plan/apply_plan` |
| B10 | **Agent registry was a dict with a silent default.** `register_default(dispatch_default)` turned *any* unknown agent type into an LLM run with no record of the decision. | `agents/registry.py`, `agents/react_agent.py:install` | **FIXED** — explicit `set_fallback` + `agent.fallback_applied` |
| B11 | **Budget was process-local.** `BudgetMonitor._spent` is a dict; a restart reset daily spend to zero. `MAX_TASK_COST_USD` was never enforced at all. | `services/model_router.py:817-836` | **FIXED** — `BudgetLedger` over `model_calls` |
| B12 | **Compaction dropped by age.** The loop discarded the *oldest* results regardless of value — dropping errors and test failures while keeping bulky listings. | `services/agent_loop.py` | **FIXED** — `ContextManager` prioritisation |
| B13 | **CI never ran.** Workflow triggered on `master`; the repository's default branch is `main`. No migration, security or frontend-build gate. | `.github/workflows/ci.yml` | **FIXED** |
| B14 | **`get_settings()` cache leaked across tests**, silently ignoring env overrides and masking real failures (13 red tests). | `tests/conftest.py` | **FIXED** |
| B15 | **API contract tests depended on an out-of-band migrated database**; schema drift surfaced as unrelated failures. | `tests/conftest.py` | **FIXED** — session migration fixture |
| B16 | **`create_planned_session` planned into a new session**, ignoring the caller's session id — the new endpoint returned 201 while writing tasks to a different session. | found by `tests/contract/test_planning_api.py` | **FIXED** |
| B17 | **`require_capability` denied only via dangerous scopes**, so a `destructive` capability could run with approvals disabled. | found by `tests/integration/test_permission_path.py` | **FIXED** |
| B18 | **Lint was not clean** (4 pre-existing errors in `api/v1/router.py`), so `make check` could not pass. | `ruff check` | **FIXED** |
| B19 | **The QA sandbox image was declared but never built.** `DockerSandbox.QA_IMAGE` and `backend/docker/qa-sandbox.Dockerfile` existed, but no Makefile target, CI step, or bootstrap path ran `docker build`. The image is on no registry, so `test_real_docker_sandbox_runs_untrusted_test` could only ever fail with an opaque `404 … pull access denied` on any machine that had not built it by hand — reported for months as "requires a Docker daemon" when the daemon was fine. | `test_real_docker_sandbox_runs_untrusted_test`, `backend/docker/qa-sandbox.Dockerfile` | **FIXED** — `DockerSandbox.ensure_image`, `make qa-sandbox-image`, CI build step |

---

## C. After — the architecture now

```text
API (/api/v1)
  → services/planner.py          goal → intent → DAG → capabilities → risk
  → services/orchestrator.py     Supervisor: validate_plan / apply_plan / schedule ready
                                 Orchestrator: AgentRun, leases, cancel, recover, complete
  → services/agent_loop.py       ReAct loop (no provider-specific code)

Worker (RQ)                      installs handlers, then dispatches by explicit resolution
  → agents/registry.py           resolve() → definition+handler, or recorded fallback
  → agents/definitions.py        CodeAgent/ResearchAgent/BrowserAgent/DocumentAgent/
                                 QAAgent/SchedulerAgent/GenericAgent policy declarations

One capability pipeline (services/tools/)
  protocol.py     ToolCall  ← provider-native OR Bob fenced  (one internal object)
  schemas.py      argument validation (JSON-Schema subset)   → invalid never reaches a handler
  execution.py    validate → authorize → run                 → the only invocation path
  permissions     require_capability → classify_risk → PermissionGate.authorize
  registry.py     Tool(name, description, parameters, risk, scope, handler, group)
  builtin/        filesystem, coding, git, shell, browser, research, documents,
                  memory, tasks, system
  optional.py     MCP + OpenConnector (only when configured)
  plugins/        third-party additions; can never shadow a first-party capability

One permission system (services/permissions.py)
  PermissionGate(factory)  → approvals table = the durable decision store
  authorize(req) → ALLOW | DENY | WAIT        (the only evaluation entry point)

One event system (domain/events.py + infra/event_bus.py)
  canonical catalog + registered extensions, validated at emit

One persistent source of truth
  SQLite: sessions, tasks, agent_runs, agent_leases, approvals, events,
          model_calls, tool_calls, artifacts, memory_notes
  services/budget.py derives daily/session/task/provider spend from model_calls
```

Dependency direction is unchanged and preserved: API → services → domain → infrastructure.
Capabilities never touch raw SQL, never open a Docker socket, and never receive a path
outside the workspace jail.

---

## D. Capability inventory

61 first-party capabilities in 10 groups (generated from the registry, not hand-written).
`permission` is the documented decision: **allowed** (read), **approval** (write/execute),
**deny** (destructive, default-deny and not approvable).

### filesystem

| name | risk | permission | scope | sandbox |
| --- | --- | --- | --- | --- |
| `file_read` | read | allowed | `file_read` | jail: allowed roots, secret paths refused |
| `file_write` | write | approval | `file:write` | jail + `max_file_size_mb` cap |
| `file_list` | read | allowed | `file_list` | jail |
| `file_search` | read | allowed | `file_search` | jail; ignored dirs skipped, 200 results |
| `file_edit` | write | approval | `file:write` | jail; refuses ambiguous/absent match |
| `file_patch` | write | approval | `file:write` | jail; unified diff, all-or-nothing |
| `file_diff` | read | allowed | `file_diff` | jail |
| `directory_tree` | read | allowed | `directory_tree` | jail; depth ≤ 10, 500 entries |
| `file_metadata` | read | allowed | `file_metadata` | jail; sha256, binary detection |

### coding

| name | risk | permission | scope |
| --- | --- | --- | --- |
| `project_detect` | read | allowed | `project_detect` |
| `repo_search` | read | allowed | `repo_search` |
| `read_source` | read | allowed | `read_source` |
| `edit_source` | write | approval | `code:write` |
| `apply_patch` | write | approval | `code:write` |
| `run_tests` / `run_linter` / `run_typecheck` / `run_formatter` | execute | approval | `coding:run_*` |
| `inspect_dependencies` | read | allowed | `inspect_dependencies` |
| `inspect_build` | read | allowed | `inspect_build` |

### git

| operation | capabilities | risk | permission |
| --- | --- | --- | --- |
| read | `git_status`, `git_diff`, `git_log`, `git_show`, `git_branch_list` | read | allowed |
| write | `git_add`, `git_commit`, `git_checkout`, `git_restore` | write | approval, scope `git:write:<repo>` |
| destructive | `git_reset_hard`, `git_clean`, `git_push_force` | destructive | **deny** (not approvable) |

### shell

| name | risk | permission | sandbox |
| --- | --- | --- | --- |
| `shell` | execute | approval (scope `shell:<normalised command>`) | Docker sandbox by default; `jail` on Heroku; `local` only by explicit opt-in; `off` disables |

### browser (interactive — distinct from research)

| name | risk | permission | scope |
| --- | --- | --- | --- |
| `browser_open`, `browser_navigate` | execute | approval | `browser:session` |
| `browser_click`, `browser_type`, `browser_select`, `browser_scroll` | execute | approval | `browser:interact` |
| `browser_extract`, `browser_screenshot` | execute | approval | `browser:extract` |
| `browser_session` | read | allowed | session listing/teardown of sessions this process created |

Payment/credential targets are refused outright (`browser:transact` and a reserved-pattern
deny list). Playwright is imported in exactly one module and is an optional dependency.

### research (provenance-preserving)

`research_search`, `research_fetch`, `research_extract`, `research_citations`,
`source_metadata`, `compare_sources` (all read/allowed), plus `web_fetch` retained as a
backward-compatible alias. Every result carries source URL + retrieval timestamp.

### documents

`document_validate`, `document_inspect`, `document_extract` (read/allowed);
`document_create`, `document_convert`, `document_persist` (write/approval, scope
`document:write`). `document_persist` registers an `Artifact` row and emits
`artifact.created`.

### memory, tasks, system

`memory_recall` (read), `memory_remember` (write/`memory:write`);
`tasks_inspect`, `task_status` (read — tasks are never mutated by an agent);
`capabilities_list`, `system_status` (read).

### conditional integrations

`openconnector_execute` (execute/`openconnector:<action>`) and `openconnector_list` (read)
appear only when `OPENCONNECTOR_BASE_URL` is set; `mcp_call` (execute/`mcp:<server>:<tool>`)
and `mcp_list` (read) only when `MCP_SERVERS` is configured. Absent integrations are absent
from the registry, so the prompt never advertises capabilities the agent cannot use.

### plugin capabilities

Third-party folders under `tools_plugins/`: execute-risk plugins run in the sandbox behind
the same gate; a plugin can never shadow a first-party capability (enforced in
`services/tools/plugins/manager.py`).

---

## E. Event inventory

**Canonical catalog** (`domain/events.py`), producers in parentheses:

| domain | events |
| --- | --- |
| session | `session.created` (API/Supervisor), `session.updated` (API), `session.completed` (Supervisor), `session.deleted` (API) |
| task | `task.created`, `task.queued`, `task.started`, `task.completed`, `task.failed`, `task.cancelled`, `task.recovering`, `task.blocked_approval` (Supervisor, Orchestrator, worker, API) |
| agent | `agent.created`, `agent.started`, `agent.waiting_tool`, `agent.waiting_approval`, `agent.completed`, `agent.failed`, `agent.terminated` (Orchestrator, worker, ReAct loop), `agent.fallback_applied` (agent registry) |
| model | `model.requested`, `model.completed`, `model.failed`, `model.token`, `model.circuit_opened`, `model.circuit_closed` (ModelRouter) |
| tool | `tool.started`, `tool.completed`, `tool.failed` (ReAct loop) |
| approval | `approval.requested`, `approval.approved`, `approval.denied`, `approval.expired` (PermissionGate, API, Telegram) |
| workspace | `workspace.created`, `workspace.modified`, `workspace.destroyed`, `workspace.restored` (API) |
| artifact | `artifact.created`, `artifact.deleted` (documents capability, API) |
| qa / recovery / recipe | `qa.*`, `recovery.*`, `recipe.*` (QA agent, Orchestrator, RecipeEngine) |
| cost | `cost.recorded`, `cost.alert` (ModelRouter, derived from persisted spend) |
| insight | `insight.generated` (InsightGenerator) |
| context | `context.compacted` (ReAct loop / ContextManager) |

**Registered extensions** (owner in parentheses): `skill.created|updated|deleted` (skills),
`a2a.delegated|result|failed` (a2a), `backup.completed|failed` (backup).

**Enforcement:** `EventBus.emit` calls `validate_event_type`; an unknown type raises
`UnknownEventTypeError`. Retired names `tool.called` / `tool.result` are *rejected*, not
aliased — a test asserts this.

---

## F. Agent inventory

| agent | execution mode | task types | required capabilities | permission policy |
| --- | --- | --- | --- | --- |
| `generic` | react | `*` | — (any) | ask |
| `code` | react | code, coding, refactor, bugfix, feature | project_detect, repo_search, read_source, edit_source, run_tests, run_linter, run_typecheck | ask |
| `research` | react | research, investigate, summarise | research_search, research_fetch, research_extract, research_citations | ask |
| `browser` | external | browser, scrape, automate | browser_open, browser_click, browser_type, browser_extract | ask |
| `documents` | external | document, documents, report | document_validate, document_create, document_persist, document_inspect | ask |
| `qa` | external | qa, test, verify | run_tests, read_source | ask |
| `scheduler` | deterministic | schedule, cron | — | default deny |
| `llm` | react | llm | — (capability library) | ask |
| `builtin` | deterministic | builtin | — | read only |

Two execution engines back nine definitions: the ReAct loop (`react`) and the honest
deterministic builtin (`deterministic`); `external` handlers are registered by their
integration module. Routing is by `agents/registry.py:resolve()`:

- registered type → its own definition (never reports a fallback);
- unregistered type + installed fallback → `generic` with
  `fallback_reason="unsupported_agent_type"`, recorded as `agent.fallback_applied`;
- unregistered type + no fallback → `UnknownAgentTypeError`.

---

## G. Recovery matrix

| failure mode | detection | behaviour | evidence |
| --- | --- | --- | --- |
| Worker killed mid-run | lease expiry (`agent_leases.lease_expires_at`) | `recovery.started` → RECOVERING → QUEUED while `attempt < 3`; `attempt` incremented only at RUNNING | `recover_orphans`, `tests/integration/test_worker.py`, `scripts/e2e_crash_recovery_check.py` |
| Retries exhausted | same | FAILED with `last_error="lease expired; retries exhausted"`, `recovery.failed` | `tests/unit/test_recovery.py` |
| API/process restart | process start | state is re-derived from SQLite; `EventBus` re-reads max sequence; approvals and budgets persist | `tests/recovery/test_persistence_restart.py`, `TestBudgetSurvivesRestart` |
| Duplicate event delivery | `event_id` dedupe in `emit` | absorbed, no sequence burned, no duplicate fanout | `tests/recovery/test_phase19_hardening.py` |
| Provider failure | consecutive `model.failed` | circuit breaker opens → fast-fail → half-open probe → close; `model.circuit_*` events | `tests/unit/test_circuit_breaker.py` |
| Model call fails inside the loop | exception in `invoke` | `stopped="error"` with last text, never raises out of the task | `tests/unit/test_react_agent.py` |
| Capability crash | handler exception | structured `capability_crash` result to the model; `tool.failed` | `tests/integration/test_canonical_events_and_capabilities.py` |
| Approval needed | `PermissionGate.authorize` → WAIT | `agent.waiting_approval` emitted; task fails honestly with the approval id; approval + retry resumes | `tests/contract/test_api_v1.py`, `TestApiApprovalUnblocksCapability` |
| Approval expires | TTL sweep | `approval.expired`; absent grant fails closed | `tests/unit/test_permission_gate.py` |
| Context overflow | token estimate vs budget | low-value results compacted, important ones retained, `context.compacted` | `TestContextRetention` |
| Planning failure | `PlanningError` / invalid DAG | session marked `PLANNING_FAILED`, 400 returned, no partial DAG executed | `tests/contract/test_planning_api.py` |
| Cancellation | user request | QUEUED → CANCELLED immediately; RUNNING sets a cancel flag checked before the handler | `Orchestrator.cancel_task` |

No retries on destructive, permission or validation failures (by design).

---

## H. Documentation reconciliation

| requirement area | status |
| --- | --- |
| One authoritative permission path | **IMPLEMENTED** — `services/permissions.py`, DB-backed; §3 DoD met |
| Canonical, enforced event taxonomy | **IMPLEMENTED** — catalog + extensions + emit validation |
| Runtime tool-argument validation | **IMPLEMENTED** — documented JSON-Schema subset |
| Capability library with real first-party tools | **IMPLEMENTED** — 61 capabilities, 10 groups |
| Coding structured rather than shell-only | **IMPLEMENTED** — 11 coding capabilities |
| Browser automation distinct from web fetching | **IMPLEMENTED** — `browser_*` vs `research_*` |
| Planner / Supervisor / Orchestrator separated | **IMPLEMENTED** — three modules, three responsibilities |
| Explicit agent registry | **IMPLEMENTED** — 9 declared definitions |
| No silent unknown-agent fallback | **IMPLEMENTED** — `UnknownAgentTypeError` unless installed, and recorded |
| Context compaction preserves important information | **IMPLEMENTED** — prioritised retention |
| Budget accounting survives restart | **IMPLEMENTED** — derived from `model_calls` |
| MCP / OpenConnector compatible | **IMPLEMENTED** — same registry, same gate, scoped approvals |
| Sandbox boundaries intact | **IMPLEMENTED** — one exec seam; no agent path to the Docker socket |
| Existing functionality operational | **IMPLEMENTED** — 605 passed, 0 failed (9 honest skips) |
| CI matches the repository layout | **IMPLEMENTED** — `main` branch, 5 jobs incl. migrations and security |
| Integration / security / recovery tests | **IMPLEMENTED** — see §I |
| Implementation docs reflect reality | **IMPLEMENTED** — this directory |
| LLM-based planning (model-generated DAGs) | **KNOWN LIMITATION** — planner is deterministic (`deterministic_keyword_v1`); plans record their strategy so a rule-based plan is never mistaken for a model-generated one |
| PDF text extraction | **KNOWN LIMITATION** — no PDF extractor in the dependency set; `document_extract` refuses rather than returning garbage |
| Browser download/upload | **KNOWN LIMITATION** — the capability layer covers open/navigate/click/type/select/scroll/extract/screenshot/session; download/upload are not implemented |
| Docker sandbox tests | **IMPLEMENTED** — the image is built by `make qa-sandbox-image` and by CI before pytest; the test runs for real and the suite is fully green (B19) |
| Real provider/model integration | **CONFIGURATION REQUIRED** — provider API keys; offline suite runs on the echo provider |
| `documentations/` product specification | **ASPIRATIONAL BY DESIGN** — remains the product vision; this directory describes what exists |

---

## I. Test evidence

All commands run from `agent-system/backend/`.

| command | result |
| --- | --- |
| `uv run ruff check src tests` | `All checks passed!` |
| `uv run ruff format --check src tests` | `153 files already formatted` |
| `uv run mypy src` | `Success: no issues found in 96 source files` |
| `make qa-sandbox-image` | image built (`agent-system/qa-sandbox:latest`) |
| `uv run pytest -q tests/unit/test_qa_agent.py::TestDockerSandboxIntegration` | **1 passed** (real container execution) |
| `uv run pytest -q` | **605 passed, 0 failed, 9 skipped** |
| `uv run alembic upgrade head` (fresh SQLite) | up to `e6f7a8b9c0d1` |
| `uv run alembic check` (fresh SQLite) | `No new upgrade operations detected.` |

The suite is now **fully green**: the formerly-failing
`test_real_docker_sandbox_runs_untrusted_test` executes inside a real container. Its long-standing
"requires a Docker daemon" label was wrong — the daemon was available and the fault was the
unbuilt image (B19). The 9 skips are honest: 5 Redis-backed worker tests, 3 optional-extra
tests (memory/telemetry), and 1 by-design read-tier assertion.

New/rebuilt test files added by this reconciliation:

| file | covers |
| --- | --- |
| `tests/integration/test_permission_path.py` | one gate; API approval unblocks a capability; restart-safe decisions; ALLOW_ONCE consumption; no capability bypasses the gate; destructive default-deny cannot be reconfigured; schema validation rejects before handlers; symlink/path escapes refused; expiry fails closed |
| `tests/integration/test_canonical_events_and_capabilities.py` | taxonomy completeness, unknown-type rejection, retired names rejected, extension ownership, bus-level enforcement, capability groups/completeness/schemas/scopes, protocol equivalence, injection sanitiser |
| `tests/integration/test_planner_budget_routing.py` | planner intents/DAG/risk, supervisor validation (cycles, unknown deps, unavailable capabilities), plan persistence in dependency order, restart-safe budgets (daily/session/task/provider/limits), explicit routing and recorded fallback, context retention, end-to-end planned execution |
| `tests/contract/test_planning_api.py` | `POST /api/v1/sessions/{id}/plan` contract, one-shot planning, dependency persistence |
| `tests/conftest.py` | settings-cache isolation + session migration fixture |
| `tests/unit/test_docker_sandbox_image.py` | image presence/provisioning contract, build-from-repo-Dockerfile, actionable failure when the build or Dockerfile is unavailable, and the deliberate split between locally-built images and registry images (B19) |

Two of the new tests found real defects in the new code before they were fixed (B16, B17) —
recorded above rather than quietly amended.

---

## Deliberately not done

- **No new subsystems for appearance.** A second event bus, a parallel permission store, an
  LLM planner, or extra agents "for naming" were all rejected; the work was to make the
  existing pieces one machine.
- **No destructive rewrites.** The database schema, migrations, `EventBus`, `ModelRouter`,
  provider adapters, RQ worker, memory system, recordings/replay, MCP, OpenConnector, and the
  dashboard/CLI contracts were preserved. Where a boundary was needed, an adapter or facade
  was added (`services/tools/plugins/manager.py`) rather than moving working code.
- **No test weakening.** Assertions were only changed where the *contract* changed and the
  reason is recorded (retired event names, restructured capability internals, jailed working
  directories). Security tests were strengthened, not relaxed: the fence-injection test now
  runs with approvals disabled so the sanitiser is the only defence being measured.
