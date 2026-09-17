# FINAL_REPORT

> **Date:** 2026-09-16 · **Scope:** agent-system build (Phases 0–21) **plus** the deep
> architecture reconciliation described in `ARCHITECTURE_RECONCILIATION.md`.
> Labels: IMPLEMENTED (code + tests green) · PARTIALLY IMPLEMENTED · CONFIGURATION REQUIRED · KNOWN LIMITATION · BLOCKED

`documentations/` remains the aspirational product specification. `docs/implementation/`
describes what actually exists. Read `ARCHITECTURE_RECONCILIATION.md` for the before/after
architecture, the divergence list, and the full capability/event/agent inventories.

## Verification Snapshot

| Check | Result |
| --- | --- |
| Backend tests | **605 passed, 0 failed**, 9 skipped |
| ruff check | `All checks passed!` |
| ruff format --check | `153 files already formatted` |
| mypy --strict | `Success: no issues found in 96 source files` |
| Frontend `tsc --noEmit` | clean |
| Frontend `next build` | clean — 17 routes |
| Migrations | `upgrade head` → `e6f7a8b9c0d1`; `alembic check` → `No new upgrade operations detected.` |
| Database | 23 tables (WAL, FK on, busy_timeout) |
| Capability library | **61 capabilities**, 10 first-party groups |
| Event taxonomy | 52 canonical types + 8 registered extensions, validated at emit |
| Agent definitions | 9 declared definitions, explicit fallback only |
| CI | `.github/workflows/ci.yml` on `main`; 5 jobs (static, tests, security, migrations, web build + smoke) |
| QA sandbox image | `make qa-sandbox-image` builds `agent-system/qa-sandbox:latest`; CI builds it before pytest |
| OpenConnector live E2E | ✓ against `ghcr.io/oomol-lab/open-connector:latest` (action exec + MCP `tools/call`) |
| Worker live E2E | ✓ out-of-process exec + kill -9 → lease reaper recovery |

The suite is **fully green**. The formerly-failing
`tests/unit/test_qa_agent.py::TestDockerSandboxIntegration::test_real_docker_sandbox_runs_untrusted_test`
now executes inside a real container. Its "requires a Docker daemon" label was wrong: the
daemon was present and the QA sandbox image had simply never been built by any target,
script, or CI step. Provisioning was wired up (`DockerSandbox.ensure_image`,
`make qa-sandbox-image`, a CI build step) — see `ARCHITECTURE_RECONCILIATION.md` B19. The
9 skips are honest: 5 Redis-backed worker tests, 3 optional-extra tests, 1 by-design tier
assertion.

## Phase-by-Phase

| Phase | Scope | Label | Notes |
| --- | --- | --- | --- |
| 0 | Scaffold, tooling, inventory | **IMPLEMENTED** | |
| 1 | Domain + persistence (WAL/FK, Alembic) | **IMPLEMENTED** | kill-9/restart tests pass |
| 2 | Event system + WS/SSE | **IMPLEMENTED** | dedupe, redaction, replay, resume; **emit-time type validation added in the reconciliation** |
| 3 | API v1 + PermissionGate + approvals | **IMPLEMENTED** | contract + security tests; **unified to one DB-backed gate in the reconciliation** |
| 4 | Supervisor DAG + Orchestrator + RQ worker | **IMPLEMENTED** + live-verified | Redis via compose; out-of-process exec ✓; kill -9 → reaper recovery ✓; **Planner split out in the reconciliation** |
| 5 | DockerSandbox + Workspaces + traversal safety | **IMPLEMENTED** | containers hardened at the boundary: `cap_drop=ALL`, `no-new-privileges`, private IPC, `noexec,nosuid` `/tmp`, pinned base image — **verified in a live container** (`tests/security/test_docker_sandbox_hardening.py`); running the daemon in production = ordinary config (see `SECURITY.md` production execution checklist) |
| 6 | Browser + research agents | **PARTIALLY IMPLEMENTED** | capability layer + jail tests green; Playwright not installed here; live capture **CONFIGURATION REQUIRED**; download/upload = **KNOWN LIMITATION** |
| 7 | DocumentAgent (PPTX/DOCX/XLSX/PDF) | **IMPLEMENTED** | deterministic builders, artifacts API; PDF *text extraction* = **KNOWN LIMITATION** |
| 8 | Obsidian vault + MemoryStore | **PARTIALLY IMPLEMENTED** | core done; LanceDB/semantic embeddings = **KNOWN LIMITATION** (hashed-lexical default, honest) |
| 9 | Dashboard (Next.js 16.3) | **PARTIALLY IMPLEMENTED** | 17 routes build; 12/14 tabs wired to live APIs; Vault + Templates tabs honest placeholders pending their list APIs |
| 10 | agentctl CLI | **IMPLEMENTED** | core command set; documents/browser/autopilot/cost CLI subcommands **KNOWN LIMITATION** (API supports them; CLI wiring pending) |
| 11 | ModelRouter + Pricing + Budgets | **IMPLEMENTED** | real providers: **CONFIGURATION REQUIRED** (keys); EchoProvider used in tests; **budget made restart-safe in the reconciliation** |
| 12 | Error recovery pipeline | **IMPLEMENTED** | |
| 13 | Autonomous QA (untrusted sandbox) | **IMPLEMENTED** | image built by `make qa-sandbox-image` + CI; real-container test passes |
| 14 | Workspace templates + secret scan | **IMPLEMENTED** | |
| 15 | Behavior recording + 3-mode replay | **IMPLEMENTED** | fingerprints + approval enforcement verified |
| 16 | Task batching + recipes | **IMPLEMENTED** | compatibility, partial failure, versioned DAGs |
| 17 | Personality + insights + scheduler | **IMPLEMENTED** | security-bounded personality; event-derived insights |
| 18 | Autopilot | **IMPLEMENTED** (service) | **off by default**; per-action approvals, sticky kill switch, audit — 17 security tests; *OS-level* executor isolation = **CONFIGURATION REQUIRED** and owned as such (production execution checklist item 5: dedicated non-privileged account, own session/display, or container/VM) |
| 19 | Hardening (chaos/restart/security) | **IMPLEMENTED** (test suite) | chaos/restart/security suites green; live-container boundary proofs added (`test_docker_sandbox_hardening.py`); full chaos matrix incl. Redis-loss/WS-flap under live infra **CONFIGURATION REQUIRED** |
| 20 | Real LLM task execution (ReAct + tool loop) | **IMPLEMENTED** | `agents/react_agent.py` — goal → ModelRouter + capability loop; worker/orchestrator wiring; honest echo fallback |
| 21 | OpenConnector + MCP over HTTP | **IMPLEMENTED** + live-verified | `services/openconnector.py` (Runtime API); `services/mcp.py` streamable-HTTP MCP; scoped approvals through the one gate; live against `ghcr.io/oomol-lab/open-connector:latest` |
| R | **Architecture reconciliation** | **IMPLEMENTED** | one permission path, canonical events, runtime schema validation, capability library (61 tools), Planner/Supervisor/Orchestrator split, explicit agent registry, restart-safe budgets, prioritised context, CI on `main`, QA sandbox image provisioning — see `ARCHITECTURE_RECONCILIATION.md` |

## Security Guarantees Verified by Tests

- **One permission path:** no capability executes without `execution.py` →
  `require_capability` → `PermissionGate.authorize`; tests attempt to bypass the gate and
  must fail closed (`tests/integration/test_permission_path.py`).
- **Destructive default-deny:** `destructive` capabilities are denied and cannot be
  reconfigured into an approvable state.
- **Schema validation before execution:** missing/unknown/wrong-typed/oversized/malicious
  arguments never reach a handler.
- Secrets: redaction at event emit, recording write, vault write, and insight persistence.
- Replay: re-execution blocked without fresh approval + fingerprint match (Phase 15).
- QA: generated tests never run in-process; sandbox or resource-limited subprocess only.
- **Container boundary hardened and live-proven:** every sandbox container runs with
  `cap_drop=ALL` (empty effective `CapEff` read from inside a real container),
  `no-new-privileges`, private IPC, `noexec,nosuid` `/tmp`, network disabled by default —
  asserted by unit tests on every `run()` and proven in a live container
  (`tests/security/test_docker_sandbox_hardening.py`).
- Autopilot: off by default, per-action approvals, hard default-deny list, sticky kill
  switch, full audit.
- Path traversal: blocked in workspace read/write/tree, symlink escapes, and template
  extraction.
- Sandbox: one exec seam; no agent path reaches the host Docker socket.
- **ReAct honesty:** offline (EchoProvider) yields a labeled "no model configured" result,
  never a simulated LLM answer.

## Honest Gaps

1. ~~**Docker-gated QA test**~~ **RESOLVED** — the image was never built; now provisioned by
   `make qa-sandbox-image` and CI. The one remaining Docker-adjacent gap is **browser
   live-capture**, which needs Playwright installed (optional dependency).
2. **Vault/Templates tab APIs** not yet exposed (services implemented server-side).
3. **Real LLM providers** need API keys; routing/costing logic is provider-aware and tested
   with adapters.
4. **LLM-based planning** is not implemented — the planner is deterministic
   (`deterministic_keyword_v1`) and records its strategy so a rule-based plan can never be
   mistaken for a model-generated one.
5. **Browser download/upload** and **PDF text extraction** are not implemented.
6. **OpenConnector connections** are connectionless-by-default; persistent named connections
   require setup in the OpenConnector runtime.
7. **E2E acceptance (§7)** should be re-run after a clean install + restart.
