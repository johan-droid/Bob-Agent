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

## Browser, research tools, and network SSRF defense

Browser navigation and research fetch capabilities are protected by a mandatory three-layer
defense-in-depth architecture to prevent Server-Side Request Forgery (SSRF) to localhost,
RFC1918 private subnets, link-local addresses, cloud metadata endpoints, CGNAT ranges, and
internal services.

```text
Layer 1: URL & IP Validation (url_security.py)
  → scheme HTTP/HTTPS, ports 80/443 only, credentials rejected
  → A & AAAA DNS resolution; fail-closed on any prohibited IP or resolution error
  → blocks RFC 1918, loopback, link-local, CGNAT, metadata (169.254.169.254, 100.100.100.200), IPv6 private/mapped

Layer 2: Playwright Browser Interception (builtin/browser.py)
  → isolated BrowserContext per session (no personal profile reuse; sandbox preserved)
  → service_workers="block"
  → context.route("**/*") intercepts all top-level navigations, redirects, popups, iframes,
    JS fetch/XHR, and subresources (images, scripts, styles)
  → fast static checks + short-TTL session host cache prevents serialized blocking DNS
  → WebSockets closed cleanly with code 1008 (Policy Violation) to avoid hanging
  → --proxy-bypass-list=<-loopback>, --disable-quic, and WebRTC UDP egress disabled

Layer 3: Pinned Outbound Proxy & Egress Boundary (browser_proxy.py & docker-compose.yml)
  → dedicated forward proxy resolves target and connects directly to pre-validated literal IP
  → TLS SNI and Host headers preserved; certificate verification enforced
  → prevents DNS rebinding (zero TOCTOU window between validation and TCP connection)
  → restricts methods to GET, HEAD, and CONNECT; supports optional Proxy-Authorization
  → packaged as dedicated container (docker/browser-proxy.Dockerfile) in docker-compose.yml
  → if proxy is unavailable, browser network access fails closed (ERR_PROXY_CONNECTION_FAILED)
```

### Layer 1: Centralized URL and IP security validator

`services/tools/url_security.py` is the single authoritative validation module used by both
browser and research tools:
- **Allowed schemes & ports:** Only `http` and `https` on standard ports `80` and `443`.
- **Credentials:** Rejects any embedded credentials (`user:pass@host`).
- **IP Range Enforcement:** Blocks loopback (`127.0.0.0/8`, `::1`), private IPv4 (`10.0.0.0/8`,
  `172.16.0.0/12`, `192.168.0.0/16`), link-local (`169.254.0.0/16`, `fe80::/10`), unique local
  IPv6 (`fc00::/7`), reserved/multicast/broadcast, and Carrier-Grade NAT (`100.64.0.0/10`).
- **Cloud Metadata:** Blocks known metadata endpoints (AWS, GCP, Azure, Oracle, DigitalOcean
  `169.254.169.254`, Alibaba `100.100.100.200`, and hostnames like `metadata.google.internal`).
- **IPv6 Representations:** Blocks IPv4-mapped IPv6 (`::ffff:x.x.x.x`), IPv4-compatible IPv6,
  NAT64 (`64:ff9b::/96`), and 6to4 (`2002::/16`) mapping to prohibited addresses.
- **Dual-Stack DNS Resolution:** Resolves both IPv4 (`A`) and IPv6 (`AAAA`) DNS records. If
  *any* resolved address is prohibited, or if DNS resolution fails, the host is rejected.
- **Normalization:** Normalizes hostnames, trailing dots, IDNs, and unusual integer/hex/octal
  numeric representations.

### Layer 2: Playwright request interception

`services/tools/builtin/browser.py` enforces runtime request interception within Playwright:
- Every session uses an isolated `BrowserContext` with `service_workers="block"`.
- Context-level routing (`context.route("**/*")`) monitors every request before it hits the
  wire, protecting main document navigations, server redirects, popups, child frames/iframes,
  and dynamic JavaScript requests (`fetch`, `XMLHttpRequest`).
- Unsafe requests are aborted with `blockedbyclient`.
- Fast static checks combined with a 60-second session host cache eliminate serialized blocking
  DNS calls on the main thread for page subresources.
- WebSockets are intercepted via `context.route_web_socket("**/*")` and closed cleanly with
  code `1008` (Policy Violation), preventing client-side connection hangs.
- Browser args include `--proxy-bypass-list=<-loopback>` so Chromium cannot bypass the proxy
  for localhost, `--disable-quic` to prevent UDP-based proxy evasion, and
  `--force-webrtc-ip-handling-policy=disable_non_proxied_udp` to eliminate WebRTC UDP leaks.
- Security sandboxing remains fully enabled (no `--no-sandbox`).

### Layer 3: Pinned outbound proxy & egress boundary

`services/browser_proxy.py` provides a dedicated outbound HTTP/HTTPS forward proxy:
- **Address Pinning & DNS Rebinding Protection:** The proxy performs DNS resolution, checks
  returned IPs via `is_prohibited_ip()`, and opens the upstream TCP connection directly to the
  validated literal IP (`_open_pinned()`). The original hostname is retained in the HTTP `Host`
  header and TLS SNI extension, preserving end-to-end TLS certificate verification while eliminating
  time-of-check to time-of-use (TOCTOU) DNS rebinding attacks.
- **Method & Protocol Controls:** The plain HTTP proxy allows only `GET` and `HEAD` methods (relaying
  unsafe mutating methods is rejected with `405 Method Not Allowed`). HTTPS traffic is handled via
  `CONNECT` after target validation.
- **Authentication:** Supports optional `Proxy-Authorization` (Bearer token or Basic auth) via
  `BROWSER_PROXY_TOKEN`.
- **Fail-Closed Availability:** If `BROWSER_PROXY_URL` is unset, browser session initialization
  refuses to start. If the proxy is stopped or unreachable, Chromium navigations fail closed with
  `ERR_PROXY_CONNECTION_FAILED`.
- **Deployment Packaging:** Built via `backend/docker/browser-proxy.Dockerfile` and run as a
  service in `docker-compose.yml`.

### Research tools hardening

`services/tools/builtin/research.py` and `agents/browser_research.py` reuse `url_security.py`:
- All URLs (`research_fetch`, `research_extract`, `research_citations`, `_source_metadata`,
  and `compare_sources`) are validated before network execution.
- HTTP requests are executed via `httpx` using `create_pinned_transport()`, ensuring the TCP
  socket connects strictly to the validated literal IP while validating TLS certificates against
  the original hostname.
- `PinnedBackend` raises `URLSecurityError` on host mismatch instead of failing open.
- Redirects are handled manually (`follow_redirects=False`) up to a maximum of 5 hops. Every
  redirect target (including relative redirects) is resolved and re-validated against all
  SSRF rules before the hop is made.
- Multi-address fallback: when a hostname resolves to multiple safe addresses (e.g. dual-stack),
  `_safe_request` attempts subsequent safe addresses on connection failure.

### Security test commands

Run the dedicated SSRF, egress isolation, and URL security test suites:
```bash
cd agent-system/backend
uv run pytest tests/unit/test_url_security.py tests/security/test_browser_ssrf.py tests/security/test_browser_egress.py
```

Run the full security test suite:
```bash
cd agent-system/backend
uv run pytest tests/security/
```

Interactive browser capabilities refuse payment/credential subjects outright
(`browser:transact` plus a reserved-pattern deny list). OpenConnector and MCP are
execute-tier with per-action scopes (`openconnector:<action>`, `mcp:<server>:<tool>`);
credentials stay in the connector runtime, never in this process.

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

**Still open (see `KNOWN_ISSUES.md`):** PBKDF2 in `services/credentials.py` uses a
hard-coded, non-per-install salt (browser SSRF vulnerability fully resolved across 3
independent defense layers).

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
