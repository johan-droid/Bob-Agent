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

### Autonomy modes

`AUTONOMY_MODE` (default `build`) decides which risk levels run **without a human**. It is
not a second authorization path — it only changes what `plan_permission` auto-approves, and
it never widens the table above:

| mode | read (LOW) | write (MEDIUM) | execute (HIGH) | destructive / `DANGEROUS_SCOPES` |
| --- | --- | --- | --- | --- |
| `build` | auto | **ask** | **ask** | **deny** |
| `plan` | auto | **deny** | **deny** | **deny** |
| `auto` | auto | auto | **ask** | **deny** |
| `unrestricted` | auto | auto | auto | **deny** |

Because `CAPABILITY_RISK_TO_RISK` maps read→LOW, write→MEDIUM, execute→HIGH and
destructive→CRITICAL, `auto` covers exactly reads and plain writes. `shell` and
`run_tests` are `execute` and therefore still ask. `plan` **refuses** write/execute rather
than deferring, because a read-only mode that merely asked would still let the model propose
edits. An unrecognised value falls back to `build`, so a typo cannot widen autonomy.

> **Threat-model note:** `unrestricted` removes the human from the loop for every
> non-default-deny capability. It is admin automation, not a default, and the destructive /
> `DANGEROUS_SCOPES` refusals above still apply in that mode. There is no `/mode` Telegram
> command yet, so the mode is set by environment only.

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
`backend/docker/qa-sandbox.Dockerfile` (pinned by digest: `python:3.12-slim@sha256:78387…184ea`
+ pytest + coverage, nothing else). It is deliberately not pulled from a registry: the image
the sandbox runs is the one in this repository's history, and the base layer cannot drift
because a mutable upstream tag moved. Rotating the pinned digest is a deliberate, reviewed
change, never silent. The image declares a non-root `USER` fallback (a caller that forgets
`user=` still does not get root); `DockerSandbox` overrides it with the invoking uid so
bind-mounted files stay host-owned, with network disabled by default and
`pids_limit`/memory/CPU caps applied.

The container runtime itself is hardened — not configurable, never relaxed by a networked
run: `cap_drop=ALL` (empty effective capability set), `no-new-privileges` (no re-exec
path to more privilege), private IPC, and a `noexec,nosuid` tmpfs on `/tmp`. These are
enforced for every container this repository starts and pinned by
`tests/security/test_docker_sandbox_hardening.py`, which includes live-daemon proofs
(effective caps read as 0 inside a real container, `/tmp` execution refused, egress blocked).

A missing local image fails closed — `SandboxUnavailableError` — and never silently falls
back to a weaker execution mode. The QA agent's subprocess fallback is opt-in
(`ExecutionMode.ISOLATED_SUBPROCESS`), never automatic.

## Production execution checklist

Architecture is one thing; a hardened deployment is an explicit set of operator choices.
Anything not checked is a residual deployment risk that this document refuses to hide:

| # | deployment requirement | status in this repo | what the operator must do |
| --- | --- | --- | --- |
| 1 | Docker daemon reachable + QA image built | code provisions it (`make qa-sandbox-image`, CI builds it, `ensure_image` on demand); missing → fail closed | run the backend on a host with Docker; do not ship where it is absent |
| 2 | Daemon itself is a trust boundary | containers: no caps, no-new-privileges, no socket, limits, pinned base image — tested live | keep the daemon patched; use rootless Docker or a dedicated daemon (e.g. Sysbox) for hostile-tenant multi-tenancy; gate `/var/run/docker.sock` exposure |
| 3 | `tools_shell_mode` is deployment configuration | default `sandbox`; `local` is an explicit host opt-in, `off` disables | in production set `TOOLS_SHELL_MODE=sandbox` (or `off`); never `local` on a shared host |
| 4 | shell mode ≠ QA sandbox | the QA agent always uses `DockerSandbox` + the QA image regardless of `tools_shell_mode` | nothing — the generated-test path is not operator-switchable to the host |
| 5 | Autopilot executor isolation | service is gated, off by default, per-action approvals, sticky kill switch; the *OS-level* isolation of the executor process is deployment packaging | run the executor process as a dedicated non-privileged OS account with no sudo, on a session/display it alone owns (or inside a container/VM); verify with `id` of the running process |
| 6 | resource ceilings fit the host | container caps come from settings | size `MAX_CONTAINER_CPU`/`MAX_CONTAINER_MEMORY_MB` to the host, not to defaults |

Items 1, 3, 4, 6 are ordinary configuration with enforced defaults. Items 2 and 5 are the
honest production-hardening boundary: the repository enforces everything inside the
container it starts, and says plainly that the daemon and the executor account are the
operator's side of the line.

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

Research fetching is guarded by `_assert_fetchable`
(`services/tools/builtin/research.py`): it resolves the hostname and refuses every
resulting address that is loopback, private, link-local, reserved, multicast, unspecified
or CGNAT (`100.64.0.0/10`, which `ipaddress.is_private` does not cover — Alibaba metadata
at `100.100.100.200`). Non-standard ports and unresolvable hosts are refused too. Checking
the host *string* alone was not enough: `127.0.0.1.nip.io` and DNS rebinding both reached
internal services before the resolver check was added. `web_fetch` and
`research_citations` now pass the guard into `ResearchAgent`, which also re-validates each
redirect hop (`follow_redirects=False`) since a public URL that 302s to
`169.254.169.254` is the standard bypass.

`file_search` enforces the same jail and denylist as `file_read` (it re-resolves every
walked path, because `rglob` follows symlinks out of the allowed roots) and scrubs its
output. The sandbox allowlist matches `argv[0]` exactly and refuses interpreter eval flags,
since a metachar blacklist let `python3 -c "import os"` through; its remaining ceiling is
documented in `sandbox.py` (a config-injection flag such as `git -c alias.x=!cmd` is not a
boundary). Vault key derivation refuses a default/empty secret. Inbound Telegram updates are
redacted before they are persisted, so a pasted SSH key never reaches the update ledger or a
backup. Approval decisions require OWNER/ADMIN and an ADMIN cannot mint `ALLOW_ALWAYS`.
`get_artifact` enforces ownership, and `autopilot_reset` requires OWNER/ADMIN (`kill` stays
open — it is a fail-safe brake).

**Still open (see `KNOWN_ISSUES.md`):** browser tools have no SSRF guard at all (real
Chromium reaches loopback and RFC1918 and follows redirects); PBKDF2 in
`services/credentials.py` uses a hard-coded, non-per-install salt.

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
