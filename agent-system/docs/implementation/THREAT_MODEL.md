# THREAT MODEL (implementation)

> **Updated:** 2026-09-16 · Assets, attackers, controls, and what is *not* mitigated.
> Controls are described in `SECURITY.md`; tests in `tests/security/` and
> `tests/integration/test_permission_path.py`.

## Assets

| asset | why it matters |
| --- | --- |
| Host filesystem outside the workspace | credentials, SSH keys, source of other projects |
| Provider API keys and integration tokens | billing, lateral movement into third-party systems |
| The workspace's own contents | user work product; destructive loss is unrecoverable |
| Approval decisions | the human-in-the-loop control; forging one defeats the security model |
| Event log | audit trail; tampering hides what the agent did |
| Cost budget | unbounded spend |
| Docker daemon / host process table | container escape, host takeover |
| Memory vault | facts the user did not intend to share |

## Trust boundaries and adversaries

1. **Untrusted model output.** The model proposes tool calls and can be steered by content it
   reads. It is *not* trusted to choose its own permissions.
2. **Untrusted remote content.** Web pages, MCP responses and connector payloads can contain
   instructions aimed at the agent (prompt injection).
3. **Untrusted third-party capabilities.** Plugin folders may be added by a user.
4. **Untrusted input to the API.** Requests may be malformed or hostile.
5. **The user/operator** is trusted with approvals, but is protected against accidentally
   granting something irreversible.

## Threats and controls

| # | threat | control | test |
| --- | --- | --- | --- |
| T1 | model invokes `shell`/`git`/writes without permission | one gate; every write/execute capability calls `require_capability`; no handler may run unapproved | `TestNoCapabilityBypassesTheGate` |
| T2 | destructive operation approved by a confused/malicious approver | `destructive` tier is default-deny and cannot be granted at any setting; `git_reset_hard`, `git_clean`, `git_push_force` declared and refused | `test_destructive_capabilities_are_default_deny`, `test_approvals_cannot_be_disabled_for_destructive` |
| T3 | approval forged or replayed across restarts/scopes | decisions are durable rows; ALLOW_ONCE consumption persisted; ALLOW_SESSION/WORKSPACE scope-checked; expiry fails closed | `TestApiApprovalUnblocksCapability`, `TestExpiryIsFailClosed` |
| T4 | path traversal / symlink escape | jail resolves symlinks before containment; secret paths refused; NUL refused | `test_malicious_path_is_refused_before_the_handler`, `test_symlink_escape_is_refused` |
| T5 | secret exfiltration (`.env`, keys) | `is_secret_path` at the jail; TTL/scoped approvals; redaction at the event boundary; `scrub_text` on capability output | `tests/unit/test_secrets.py`, `tests/security/test_workspaces.py` |
| T6 | prompt injection re-arming a tool call from fetched content | tool results are sanitised (`sanitize_tool_result`) before re-entering the prompt; fenced-regex parsing is neutralised; approvals are unaffected | `tests/security/test_tool_fence_injection.py` (runs with approvals **off**, so the sanitiser is the only defence measured) |
| T7 | container escape / host takeover | one execution seam; Docker sandbox with CPU/memory/process/output limits by default; no capability receives a Docker socket; `local` requires explicit opt-in | `tests/unit/test_subprocess_jail.py`, `tests/security/test_workspaces.py` |
| T8 | resource exhaustion (fork bombs, disk fill, runaway output) | rlimits in the jail, sandbox limits, `max_file_size_mb`, output caps, timeouts, iteration budget, context budget | `tests/unit/test_subprocess_jail.py`, `tests/unit/test_context_compaction.py` |
| T9 | malformed/hostile tool arguments | schema validation before the handler: types, required, unknown-key rejection, enums, patterns, size cap | `TestSchemaValidationBeforeExecution` |
| T10 | unauthorized API access | bearer session token from a bootstrap secret; default secrets refused in production; CORS restricted | `tests/contract/test_api_v1.py` |
| T11 | event tampering / lost audit | append-only store, monotonic sequence, dedupe by `event_id`, type validation, redaction at emit | `tests/integration/test_event_bus_persistence.py` |
| T12 | third-party plugin escalating privileges | plugins cannot shadow first-party capabilities; execute-risk plugins run in the sandbox behind the same gate | `tests/unit/test_tool_plugins.py` |
| T13 | unbounded cost | budgets derived from persisted model calls (daily/task tokens/task cost) fail the call fast; circuit breaker prevents retry storms | `TestBudgetSurvivesRestart` |
| T14 | rogue automation (payments, account changes) | `browser:transact` default-deny; reserved-pattern refusal for payment/credential subjects; autopilot off by default with per-action approvals and a sticky kill switch | `tests/security/test_autopilot.py` |
| T15 | unbounded planning / recursion | plan task limit, duplicate-key rejection, unknown-dependency rejection, cycle rejection, capability existence check | `TestSupervisorPlanValidation` |
| T16 | silent capability substitution | unknown agent type raises unless a fallback is installed, and any fallback that runs emits `agent.fallback_applied` | `test_fallback_decision_is_recorded_as_an_event` |
| T17 | unsafe archive extraction / template cloning | template snapshots are scanned for secrets before snapshot; clone fidelity tested | `tests/unit/test_features_16_17.py`, `tests/contract/test_vault_and_templates_api.py` |

## Residual risk (accepted, documented)

| risk | why it remains | mitigation direction |
| --- | --- | --- |
| `tools_shell_mode=local` executes on the host | opt-in deployment choice for platforms without Docker | approvals still apply; document loudly (`SECURITY.md`) |
| The subprocess jail is containment, not isolation | rlimits + scrubbed env + cwd confinement cannot stop a determined escape | Docker sandbox is the default; jail is the no-daemon fallback |
| The deterministic planner may choose a mediocre DAG | no LLM planner exists | plan is inspectable and stored (`strategy` field) before any execution; `KNOWN LIMITATION` in the reconciliation report |
| A model can still ask for a large number of *allowed* read capabilities | reads are not approval-gated by design | iteration budget, context budget, `tool.started/completed` audit trail, cost accounting |
| Prompt injection can change the model's *plan*, not its permissions | permissions are enforced outside the model | keep expanding validation + approval scope fidelity |
| Secrets inside content the agent legitimately reads (not a secret *path*) | content-level detection is heuristic | `scrub_text` + event redaction; no perfect control |

## Review triggers

Re-review this model when: a new capability group is added, a new execution backend is
introduced, `DANGEROUS_SCOPES` changes, a new event producer is added, or the planner
becomes model-driven.
