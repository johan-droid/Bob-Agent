# SECURITY (implementation)

> **Updated:** 2026-09-16 · One permission system, one execution seam, one path jail.
> Threat analysis and residual risk: `THREAT_MODEL.md`.

## One authoritative permission path

`services/permissions.py` is the only place a capability can learn whether it may run.

```text
capability request
  → classify_risk(tier, scope)     read→LOW  write→MEDIUM  execute→HIGH  destructive→CRITICAL
                                   dangerous scope escalates to CRITICAL
  → PermissionGate.check           durable ALLOWED grant, unexpired, policy-scoped?
  → PermissionGate.request         otherwise persist PENDING with a TTL
  → authorize() → ALLOW | DENY | WAIT
  → executor
```

- **Durable.** With a session factory the gate reads and writes the `approvals` table, so a
  decision made through `POST /api/v1/approvals/{id}/decision` is visible to the capability
  that requested it, in any process, after a restart. (Previously the API gate was
  in-memory and the tools used separate DB helpers — approvals never connected.)
- **Single.** `require_capability()` is the only entry point; `tools/execution.py` calls it
  for every capability. Tools, plugins, MCP and OpenConnector share it.
- **Fail-closed.** Expiry denies, missing grants deny, and a store error denies.
  ALLOW_ONCE is consumed atomically and the consumption is persisted.

### The documented decision table

| capability tier | decision | notes |
| --- | --- | --- |
| `read` | allowed | no approval; still jailed and secret-filtered |
| `write` | approval | scope-bound; `tools_require_approval=false` may disable |
| `execute` | approval | scope-bound; sandboxed regardless |
| `destructive` | **deny** | not approvable by anyone, at any setting |

### Policies

`ALLOW_ONCE` (consumed on first use), `ALLOW_SESSION` (session-scoped), `ALLOW_WORKSPACE`
(workspace-scoped), `ALLOW_ALWAYS` (durable), `DENY`. Risk TTLs: LOW 60 min, MEDIUM 30,
HIGH 15, CRITICAL 5. `approval.expired` is emitted by the sweep.

### Default-deny scopes

`DANGEROUS_SCOPES`: `host:filesystem`, `host:shell`, `host:credentials`, `host:users`,
`host:firewall`, `host:bootloader`, `host:security_software`, `browser:transact`,
`credential:transmit`, `autopilot:input`, `a2a:delegate`. A request for any of these is
recorded as DENIED and can never be approved.

## Workspace jail

`services/tools/paths.py` is the single reachability decision:

- requested paths resolve against allowed roots (`workspaces/`, `outputs/`, the process CWD,
  plus `TOOLS_FS_ROOTS`);
- symlinks are resolved **before** the containment check, so a link pointing outside the
  roots is refused, not followed;
- secret-looking paths (`is_secret_path`) are refused, including inside a jail root;
- NUL bytes are refused; `max_file_size_mb` caps reads and writes.

## Execution and sandbox

`services/tools/builtin/_exec.py:run_workspace_command` is the only seam that starts a
process:

| mode | behaviour |
| --- | --- |
| `sandbox` (default) | `DockerSandbox` with CPU/memory/process/output limits |
| `jail` (or `HEROKU_JAIL=true`) | `SubprocessJail`: rlimits, scrubbed env, timeout, output caps, optional binary allowlist |
| `local` | host execution, explicit opt-in only, still behind the gate |
| `off` | no process execution at all |

No capability receives a Docker socket. `shell` is jailed to the same allowed roots as every
other capability (its working directory is resolved through the jail).

### Image provenance

Untrusted generated tests run in `agent-system/qa-sandbox:latest`, built from the in-repo
`backend/docker/qa-sandbox.Dockerfile` (`python:3.12-slim` + pytest + coverage, nothing
else). It is deliberately not pulled from a registry: the image the sandbox runs is the one
in this repository's history. Sandbox containers run as the invoking uid so bind-mounted
files stay host-owned, with network disabled by default and `pids_limit`/memory/CPU caps
applied.

A missing local image fails closed — `SandboxUnavailableError` — and never silently falls
back to a weaker execution mode. The QA agent's subprocess fallback is opt-in
(`ExecutionMode.ISOLATED_SUBPROCESS`), never automatic.

## Tool argument validation

Arguments are validated against the tool's published JSON schema before the handler is
entered (`services/tools/schemas.py`): types, `required`, `additionalProperties: false`
(unknown arguments rejected), `enum`, `const`, `pattern`, length/min/max, nested objects,
array `items`, and a hard 100 000-character payload cap. A validation failure returns a
structured error to the model instead of reaching the capability.

## Secret handling

- `redact_payload` scrubs secret-looking keys at the event boundary.
- `services/secrets.py` identifies secret paths; `services/memory.scrub_text` scrubs text
  that leaves a capability.
- Prompt composition, events, recordings, templates and error messages are all downstream of
  those boundaries.
- `Settings` refuses to start with default dev secrets when `AGENT_ENV=production`.

## Browser and network

Interactive browser capabilities refuse payment/credential subjects outright
(`browser:transact` plus a reserved-pattern deny list). OpenConnector and MCP are
execute-tier with per-action scopes (`openconnector:<action>`, `mcp:<server>:<tool>`);
credentials stay in the connector runtime, never in this process.

## Plugins

Third-party capabilities register through the same pipeline, cannot shadow a first-party
capability (`services/tools/plugins/manager.py`), and an execute-risk plugin runs in the
sandbox behind the same gate.

## Frontend/CLI

Both consume the same `/api/v1` contracts; stream authentication is required. There is no
CLI-only business logic and no direct database access from the UI.

## Security test coverage

`tests/security/` (jail routing, allowlist, autopilot, tool-fence injection, workspace
boundaries) plus `tests/integration/test_permission_path.py` (bypass attempts, default-deny,
schema rejection, path/symlink escape, expiry). CI runs `tests/security` as its own gate.
