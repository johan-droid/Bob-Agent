# Decisions & Deviations

Log of engineering decisions and deviations from `docs/DEVELOPER_GUIDE.md` made
during the hardening pass (P0–P3). New entries appended as work proceeds.

## Baseline test-suite status (2026-09-07, pre-change)

`make check` / `pytest -q` against `master` before any hardening edits:

- **422 passed, 5 skipped, 1 failed.**
- The single pre-existing failure is:
  `tests/unit/test_qa_agent.py::TestDockerSandboxIntegration::test_real_docker_sandbox_runs_untrusted_test`
- **Classification: out-of-scope pre-existing bug, NOT caused by hardening work.**
  Docker daemon is healthy (`docker info` OK) and the `agent-system/qa-sandbox:latest`
  image is present. The failure is a latent bug in the QA sandbox flow: the test stages
  `mymod.py` into a `staging` tempdir, but `QAAgent.run_generated_tests` creates its own
  internal `tmp_dir`, copies only the generated test file there, and mounts that
  `tmp_dir` at `/ws` — so `import mymod` (the module under test) fails to collect
  inside the container (collection error, `tests_executed == 0`).
  `QAAgent._run_in_sandbox` (`agents/qa.py`) does not mount
  `module_search_path` into the container, so the source module is genuinely
  unreachable in sandbox mode.
- **Decision:** This is a feature-level bug in the QA agent, outside the P0–P3
  hardening scope (none of which is tasked with fixing QA-sandbox module visibility).
  It is **not** a security test and is **not** in `tests/security/`, so the rule
  "do not weaken existing security tests" is unaffected. The hardening green-baseline
  is therefore interpreted as **"no new regressions"** — all 422 currently-passing
  tests must remain green after my changes, and this pre-existing failure must not be
  silently masked or weakened.
- **Follow-up tracked:** fix QA sandbox to make `module_search_path` (source dir)
  available inside the container (mount it read-only alongside `/ws`, or copy the
  module under test into `tmp_dir`), then re-enable the assertion. Not done in this pass.

## P0 — Tool-fence injection filter (IMPLEMENTED)

- **What:** Added `sanitize_tool_result(text)` in `services/agent_loop.py` that
  inserts an invisible zero-width space (U+200B) after any opening ` ```tool:`
  fence in untrusted tool output, before it is appended to the transcript as a
  `<tool_result>` block. This breaks `TOOL_FENCE_RE` matching so the fence cannot
  be re-parsed as a live tool call on the next iteration, while the rendered text
  stays visually identical.
- **Why:** `web_fetch` / `mcp_call` results flow back into the model prompt. A
  fetched page or MCP response carrying a live ` ```tool:shell {...}`` fence is a
  prompt-injection vector if the model forwards it verbatim.
- **Scope discipline:** Only the tool-result → prompt path is sanitized. The
  model's own outgoing fence parsing (`parse_tool_calls`) is untouched, so
  legitimate tool calls still work (verified by a regression test).
- **Tests:** `tests/security/test_tool_fence_injection.py` (7 tests) — unit
  guarantees on the sanitiser + an end-to-end loop test proving an injected
  shell fence is NOT executed when the model echoes tool output, plus a test
  confirming legitimate calls still parse.
- **Verification:** 429 passed / 1 pre-existing failure. ruff + ruff format +
  mypy --strict all clean.

## Reversal — QA sandbox module visibility (FIXED, was: out-of-scope)

- **What changed:** The baseline entry above classified
  `test_real_docker_sandbox_runs_untrusted_test` as out-of-scope. During this
  pass it was fixed anyway, in `agents/qa.py:QAAgent._run_in_sandbox` + the
  test's monkeypatch.
- **Why:** Two hardening gates require it: "do not advance with a red suite"
  and "CI green on current master" (GitHub `ubuntu-latest` runners ship a
  Docker daemon, so the test runs — not skips — in CI; the new
  `.github/workflows/ci.yml` would be red on arrival).
- **Fix (no weakening):** the container mounts the run dir at `/ws`, so the
  real `_run_in_sandbox` now stages top-level `*.py` from `module_search_path`
  next to the generated test (bounded copy, documented) and targets `/ws`
  instead of the host path; the test's monkeypatch mirrors that staging. Same
  real-container execution, same `tests_executed >= 1` assertion — now passing
  (verified against a live `agent-system/qa-sandbox:latest` run).
- **Residual note:** staging is top-level `*.py` only; packages/subdirectories
  under test are still not visible in sandbox mode — tracked as a known
  limitation, not a regression.

## P1 — Memory embeddings (IMPLEMENTED)

- `LocalEmbeddingProvider` (`all-MiniLM-L6-v2`) + `memory` extra in
  `pyproject.toml`; `MEMORY_EMBEDDING_PROVIDER` (`hash` default, `local`,
  else fails closed); `hash` path byte-for-byte untouched.
  Semantic-superiority test skips without the extra (honest, not faked).

## P1 — Context compaction (IMPLEMENTED)

- `MAX_CONTEXT_TOKENS` (100_000) + `CONTEXT_COMPACTION_THRESHOLD_PCT` (75%)
  in `Settings`; chars/4 heuristic documented as approximation in
  `estimate_tokens()`; `context.compacted` event; `react_agent` wires settings
  through. Single-setting (not per-provider table) — per-provider budgets
  recorded as future work.

## P1 — Circuit breaker (IMPLEMENTED)

- `ProviderCircuitBreaker` in `model_router.py`, wired into both `invoke` and
  `invoke_streaming`; fast-fail still records a failed `ModelCall`
  (preserves the router's every-call-recorded invariant). Threshold/cooldown
  via `Settings`, passed through `build_model_router`. `invoke` was refactored
  onto shared `_compose/_run_guarded/_record` helpers — behavior verified
  identical by the untouched existing router/recovery tests.

## P1 — Backups (IMPLEMENTED)

- `services/backup.py` (online SQLite backup API + tar reusing
  `secrets.is_secret_path`, not a duplicated predicate); retention prune;
  APScheduler nightly job wired in `api/main.py:lifespan` (new — no scheduler
  existed); `agentctl backup run|list|restore` implemented as **local-ops**
  commands (settings-CLI pattern, not API round-trips) because backups touch
  local files and `restore` must refuse a *live server*; liveness = TCP probe
  on `api_port` (documented heuristic).

## P2 — Pluggable tools (IMPLEMENTED)

- New `services/tool_plugins.py` (skill-pattern discovery + `.state.json`);
  single `handle(args) -> dict` protocol; execute-risk plugins always
  approval-gated + always `DockerSandbox` (a staged stdlib runner;
  `tools_shell_mode=local` explicitly does not apply); read/write run
  in-process like their built-ins; built-in shadowing refused.
  `agentctl tools list|enable|disable` are local (plugin state is local
  files); the server picks up toggles per-task with no restart.

## P2 — Token streaming (IMPLEMENTED)

- `stream()` on OpenAI-compatible + Anthropic adapters (`(chunks, usage)`
  contract) + `EchoProvider` word-chunks; `ModelRouter.invoke_streaming`
  alongside (not replacing) `invoke`, sharing `_record` so accounting is
  identical; `model.token` (visibility=user, persisted) → existing WS/SSE
  fanout unchanged; `agentctl chat` inline rendering; dashboard chat live
  bubble with `model.completed` fallback. Deviation: `agent_loop.py` itself
  needed no signature change — tokens flow through the loop's existing `emit`
  channel from the streaming invoke closure (documented in §8.30).

## P2 — Metrics & tracing (IMPLEMENTED)

- `infra/telemetry.py`: endpoint unset ⇒ no OTel imports, only cheap
  in-memory counters (always on — negligible overhead, makes wiring
  assertable offline); endpoint set + extra installed ⇒ OTLP + FastAPI/
  SQLAlchemy/httpx auto-instrumentation; missing extra ⇒ disabled with
  warning, never fatal. Wired at router (latency/cost), agent loop (tool
  counts), approvals endpoint (decision latency), worker (task durations).

## P2 — CI (IMPLEMENTED)

- `.github/workflows/ci.yml`: `make check` job (py3.12, uv cache) + web
  smoke job (node 20, npm cache, headless Chromium via playwright-core).
  No secrets. Deliberate-break verification done and reverted. Note: the repo
  was not `ruff format`-clean at baseline, so `make check` was red before
  this pass — a tree-wide `ruff format` (mechanical only) was applied to get
  the gate green;backend behavior unchanged (suite green before and after).

## P3 — UI tokens/theme/a11y/responsive (IMPLEMENTED with deviation)

- **Deviation:** the prompt assumes Tailwind (`tailwind.config.*`), but the
  repo reality is plain CSS (`web/src/app/globals.css`, no Tailwind dep).
  Per the source-of-truth hierarchy, tokens were implemented as CSS custom
  properties (`--background/surface/border/text-primary/secondary/accent/
  success/warning/danger/info` + dark overrides), not a Tailwind config.
- Theme: `data-theme` + `bob-theme` cookie (not localStorage) +
  `prefers-color-scheme` default with pre-paint script (no flash). All
  text/background pairs contrast-checked ≥ 4.5:1 (audit table in
  `docs/HARDENING_PASS_REPORT.md`). Sidebar → drawer under 768px;
  `focus-visible` rings; `aria-live` token bubble. Smoke test extended:
  HTTP markup asserts + real headless-Chromium round-trip (toggle flips
  `data-theme`, cookie persists, 375px drawer affordance) — verified green
  locally with playwright-core + chromium.

## P3 — A2A handoff (IMPLEMENTED)

- `services/a2a.py` + `api/v1/a2a.py`, off by default. Gating mirrors
  autopilot: `a2a:delegate` added to `DANGEROUS_SCOPES` (never ALLOW_ALWAYS);
  concrete delegations need a fresh APPROVED record bound to
  `a2a:delegate:<host>:<task_id>` (Risk HIGH), else 409 + approval id, zero
  network. HMAC-SHA256 envelopes (pre-shared `API_SESSION_SECRET`,
  10-min freshness); callback is signature-authed (Telegram-webhook pattern).
  Result flips the task RUNNING→SUCCEEDED with `result_json` + `a2a.result` /
  `task.completed` events.

