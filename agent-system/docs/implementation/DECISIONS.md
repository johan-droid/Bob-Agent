# DECISIONS (implementation)

> **Updated:** 2026-09-16 · Decisions made while reconciling the architecture.
> Product-level decision history remains in `documentations/22_Decision_Log.md`.

## D-01 — One permission gate, durable, two stores behind one interface

**Decision.** `PermissionGate` is the only evaluator. It is DB-backed when constructed with
a session factory (the API, worker and capabilities all pass one) and in-memory otherwise
(unit tests, detached analysis, autopilot). Both stores implement the same `ApprovalStore`
protocol, so semantics do not differ — only lifetime.

**Why.** The system previously had an in-memory gate for the API *and* separate DB helpers
inside the tool layer. An approval granted in the UI could never unblock the tool, and the
gate's records vanished on restart. Rather than pick one and break the other's callers, one
interface absorbed both.

**Cost.** `test_permission_gate.py`'s in-memory expectations are preserved unchanged;
production paths additionally persist. A new migration added the columns the durable store
needs (`policy`, `consumed`, `session_id`, `workspace_id`, `context_json`).

## D-02 — Capability risk is a tier; the level mapping is central

**Decision.** Tools declare `read`/`write`/`execute`/`destructive`. The mapping to
`LOW`/`MEDIUM`/`HIGH`/`CRITICAL` lives only in `classify_risk`, with dangerous scopes
escalating to `CRITICAL`.

**Why.** Tools previously had no way to express risk in the gate's vocabulary, so the two
taxonomies drifted. A central mapping means a tool cannot lower its own risk — it can only
declare a tier, and an unknown tier fails safe to `HIGH`.

## D-03 — `destructive` is default-deny, on the tier, not only the scope

**Decision.** A destructive capability is refused regardless of `tools_require_approval`.
Its scope is recorded and the request is audited as DENIED, but no approval can grant it.

**Why.** The first implementation denied only via `DANGEROUS_SCOPES`, so a destructive tool
with a non-listed scope ran when approvals were switched off. `tests/integration/test_permission_path.py`
caught it before it shipped; the fix moved the rule onto the tier where the intent lives.

## D-04 — Event types are validated at emit; extensions are declared, not invented

**Decision.** `EventBus.emit` raises `UnknownEventTypeError` for a type outside the catalog.
Subsystem events are registered once with an owner (`register_event_extensions`).
Canonical product/operational events were added to the catalog (`model.token`,
`context.compacted`, `cost.alert`, `agent.fallback_applied`, `session.updated/deleted`,
`workspace.restored`, `model.circuit_*`).

**Why.** ~12 event names were being emitted but were absent from the catalog; nothing
validated them, so a typo or an invented name silently entered the audit trail. Strict
validation converts that into a loud failure at development time. Retired
`tool.called`/`tool.result` are rejected rather than aliased, so a stale producer cannot
quietly keep working.

## D-05 — `tool.started`/`tool.completed`/`tool.failed` replace `tool.called`/`tool.result`

**Decision.** Rename rather than alias.

**Why.** The canonical catalog already declared the three-state lifecycle; keeping the old
names would have meant two vocabularies for one concept. One test and two docs were updated.

## D-06 — One execution path; capabilities declare scope, never policy

**Decision.** `tools/execution.py` performs validate → authorize → run for every capability.
Approval checks were removed from the handlers (`_shell`, `_openconnector_execute`,
`_mcp_call`) and from `_require_plugin_approval`.

**Why.** Four copies of approval logic could drift independently, and each was a place to
forget a check. Scope resolution is now a `Tool` field (constant or callable), so a
per-call scope like `shell:<command>` or `git:write:<repo>` is declarative.

## D-07 — JSON-Schema subset, implemented in-tree

**Decision.** `tools/schemas.py` implements the subset the library actually authors
(`type`, `required`, `properties`, `additionalProperties`, `enum`, `const`, `pattern`,
length/min/max, `items`, `minItems`/`maxItems`, `minProperties`/`maxProperties`, `oneOf`).
Anything outside the subset is treated as unconstrained.

**Why.** No JSON-Schema validator is in the dependency set, and adding a dependency for it
was not justified when the schema dialect is ours and small. The subset is documented and
tested; unknown *keywords* are ignored while unknown *arguments* are rejected, which is the
behaviour the security tests need.

## D-08 — Protocol abstraction, fenced protocol retained

**Decision.** `tools/protocol.py` parses both provider-native structured tool calls and Bob's
fenced blocks into one `ToolCall`. Native wins when present; fenced is the universal
fallback.

**Why.** Deleting the fenced protocol would break every adapter (including echo/offline
mode) and all recorded transcripts. The abstraction makes execution protocol-agnostic and
lets a provider's native tool-calling be used when it exists.

## D-09 — Planner is deterministic and says so

**Decision.** `services/planner.py` is rule-based
(`PLANNING_STRATEGY = "deterministic_keyword_v1"`). No model call. The strategy string is
part of every persisted plan and the API response.

**Why.** The planning step had to exist and be inspectable now; pretending an LLM planned
would be fake production behaviour. Recording the strategy means a rule-based plan is never
mistaken for a model-generated one, and an LLM planner can be added later without changing
the contract.

## D-10 — Agent fallback is explicit and recorded

**Decision.** `register_default` became `set_fallback(handler, reason=...)`.
`resolve()` raises `UnknownAgentTypeError` when no fallback is installed; when one is, the
resolution carries `fallback_reason="unsupported_agent_type"` and
`agent.fallback_applied` is emitted.

**Why.** The previous silent default meant an unrecognised `agent_type` quietly became an
LLM run with no trace of the substitution — invisible routing, explicitly forbidden by the
contract. The deterministic builtin remains the honest handler for goal-less tasks.

## D-11 — Budget derived from persisted model calls

**Decision.** `services/budget.py:BudgetLedger` aggregates the `model_calls` table for
daily/session/task/provider scopes and enforces `DAILY_BUDGET_USD`, `MAX_TASK_COST_USD`,
`MAX_TASK_TOKENS`. `model_calls` gained `session_id`. `cost.alert` thresholds are computed
from persisted spend (before = after − this call), so they fire once and survive a restart.

**Why.** `BudgetMonitor` was a process-local dict; a restart reset the day's spend to zero,
and `MAX_TASK_COST_USD` was never enforced. The database already recorded every call — the
fix was to read it instead of counting in memory. `BudgetMonitor` remains for compatibility
and for factory-less use.

## D-12 — ContextManager prioritises instead of dropping by age

**Decision.** Results are classified (`CRITICAL` for errors/denials, `HIGH` for changes,
test/lint/typecheck and state inspection, `LOW` for bulk read output), pinned when
important, and only low-value results are compacted. The chars/4 estimator remains the
documented fallback; system messages and the task are never compaction candidates.

**Why.** Dropping the oldest result is exactly backwards: the oldest result is frequently
the error or constraint the agent is working around.

## D-13 — `services/tools/` package with a compatibility facade

**Decision.** The former `services/tools.py` module became a package
(`registry`/`schemas`/`execution`/`protocol`/`paths`/`optional`/`builtin`/`plugins`), and
`services/tools/__init__.py` re-exports the previous public names.

**Why.** The library needed real seams; the ~15 existing import sites did not need to change.
`services/tool_plugins.py` (377 lines of working code) was kept where it was and reached
through `services/tools/plugins/manager.py` rather than being moved — adapters over
rewrites.

Private names used by tests (`_shell`) moved to their group module and the four call sites
were updated; no assertion was weakened.

## D-14 — Jail the shell working directory

**Decision.** `cwd` for process execution is resolved through the same path jail as file
capabilities.

**Why.** File tools were jailed but shell could be pointed anywhere, which made the jail
decorative. This is a behaviour change: tests that pass a `tmp_path` working directory now
declare it as an allowed root (`TOOLS_FS_ROOTS`), which is also the documented operator
workflow.

## D-15 — CI targets `main` and gates the things that were silently unchecked

**Decision.** Five jobs: static (ruff + mypy strict), tests, security suite, migrations
(`upgrade head` + `alembic check` + idempotent re-upgrade), web (typecheck + build + smoke).

**Why.** The workflow triggered on `master` in a repository whose default branch is `main`,
so it had never run. Migration/model drift and the security suite were unchecked.

## D-16 — Test isolation fixes, not test weakening

**Decision.** (a) An autouse fixture clears the `get_settings()` cache around every test.
(b) A session-scoped fixture brings `settings.database_url` to head with real migrations.
(c) Assertions were changed only where a *contract* changed, with the reason recorded in the
diff and in `ARCHITECTURE_RECONCILIATION.md`.

**Why.** 13 red tests shared one root cause: a process-wide settings cache silently ignoring
env overrides, which hid a genuine integration problem. The security fence-injection test was
*strengthened*: it now runs with approvals disabled so the sanitiser is the only defence
being measured.

## D-17 — Locally-built sandbox images are provisioned, not assumed

**Decision.** `DockerSandbox` distinguishes images this repository builds
(`LOCAL_IMAGES`, currently only `agent-system/qa-sandbox:latest`) from registry images.
Locally-built images are provisioned explicitly — `make qa-sandbox-image`, a CI build step
before pytest, or `DockerSandbox.ensure_image(image)` to build on demand. A missing local
image raises `SandboxUnavailableError` naming the fix; a start failure for a registry image
such as `python:3.12-slim` stays a generic `SandboxError`.

**Why.** The QA image was declared in three places and built in none, so the Docker-gated QA
test failed with an opaque `404 … pull access denied` on every machine that had not built it
by hand — misread for months as "needs a Docker daemon" when the daemon was fine. Two
behaviours were deliberately rejected: silently pulling from a registry (the sandbox must run
the image in this repository's history), and falling back to weaker isolation (a missing image
fails closed; the subprocess fallback stays opt-in). The failure message differs by image
class because "run the build" is the wrong advice for an image Docker can pull itself.

## D-18 — The Docker boundary is hardened in code; the daemon and executor account are owned deployment concerns

**Decision.** After the sandbox *architecture* was proven (in-repo image, fail-closed, no
in-process untrusted execution), the *runtime* was hardened too, as non-configurable code:
every container `DockerSandbox` starts runs with `cap_drop=ALL`, `security_opt=no-new-privileges`,
private IPC, and a `noexec,nosuid` tmpfs on `/tmp`; the QA image's base layer is pinned by
digest and falls back to a non-root `USER` if a caller ever forgets `user=`. Only the network
posture is switchable (`network=True`), and it relaxes nothing else. These properties are
pinned by `tests/security/test_docker_sandbox_hardening.py` (unit assertions on every `run()`
call plus live-daemon proofs: `CapEff` read as 0 inside a real container, `/tmp` execution
refused, egress blocked).

**Why.** The container boundary was reported as "configuration-dependent" because the Docker
daemon is a trust boundary no in-repo control can eliminate — but that true statement had
become an excuse for leaving the daemon side unhardened and for letting Autopilot's
"restricted OS account" exist only in the aspirational spec. The line is now drawn explicitly
(`SECURITY.md` → "Production execution checklist"): the repository enforces everything inside
the containers it starts and names exactly two operator-owned deployment items — keep/derive
the daemon from a hardened runtime (rootless Docker / dedicated daemon for hostile tenants),
and run the Autopilot executor as a dedicated non-privileged OS account on a session it alone
owns (or in a container/VM). A report that says "deployment concern" without an operator
checklist is deflection, not documentation.
