# Hardening Pass Report (2026-09-07, autonomous)

Source: `DEVELOPER_GUIDE.md` audit → this report. Convention follows the v3.1
`FINAL_REPORT.md` (IMPLEMENTED / PARTIALLY IMPLEMENTED / CONFIGURATION
REQUIRED / KNOWN LIMITATION / BLOCKED). Detail/rationale per task lives in
`docs/DECISIONS.md`; implementation references in `docs/DEVELOPER_GUIDE.md` §8.

## Gate status (final)

- `make check` (ruff + `ruff format --check` + `mypy --strict` + pytest):
  **green** — `484 passed, 8 skipped, 0 failed` (492 collected).
- Baseline at pass start: 429 passed / 5 skipped / 1 failed (pre-existing
  `test_real_docker_sandbox_runs_untrusted_test`, since fixed — see below).
- `web/smoke-test.mjs`: all checks pass, including the headless-Chromium
  theme round-trip (verified locally with playwright-core + chromium).
- `.github/workflows/ci.yml` added (backend gate + web smoke jobs, no
  secrets). Deliberate-break verification performed and reverted. The workflow
  itself has not executed on GitHub from this environment — first push will be
  its maiden run (both jobs mirror locally-verified commands).

## Task labels

| # | Task | Label | Notes |
|---|------|-------|-------|
| P0 | Tool-fence injection filter | IMPLEMENTED | Landed before this session; verified green (`test_tool_fence_injection.py`). |
| P1 | Memory: real embeddings | IMPLEMENTED | `hash` default untouched; `local` opt-in via `memory` extra. |
| P1 | Context/token compaction | IMPLEMENTED | Heuristic documented; `context.compacted` event. |
| P1 | Circuit breaker per provider | IMPLEMENTED | Trip/fast-fail/probe/redouble all tested. |
| P1 | Backup cadence | IMPLEMENTED | Service + scheduler job + `agentctl backup` CLI. |
| P2 | Pluggable tool registration | IMPLEMENTED | Same safety model; sandbox-forced execute; sample plugin. |
| P2 | Token-level streaming | IMPLEMENTED | New path; identical accounting; graceful fallback. |
| P2 | Metrics & tracing | IMPLEMENTED | Disabled-by-default; OTel path needs extra. |
| P2 | CI pipeline | IMPLEMENTED | Added; maiden GitHub run still pending (see above). |
| P3 | UI tokens/theme/a11y/responsive | IMPLEMENTED | Plain-CSS tokens (no Tailwind in repo — deviation documented). |
| P3 | A2A handoff | IMPLEMENTED | Single-provider round-trip, approval-gated, off by default. |

## CONFIGURATION REQUIRED (operator action, by design)

- `local` embeddings: `pip install agent-system[memory]` +
  `agentctl settings set MEMORY_EMBEDDING_PROVIDER local`.
- Nightly backups run only when the API server runs with
  `SCHEDULER_ENABLED=true` (default) — no separate daemon needed.
- Telemetry export: `pip install agent-system[telemetry]` +
  `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` + observability compose
  profile in `docs/OPERATIONS.md`.
- A2A: `A2A_ENABLED=true`, a reachable external agent URL, and a pre-shared
  `API_SESSION_SECRET` on both sides (result signatures).
- Dashboard theme: no action — OS default first, cookie persists the toggle.

## KNOWN LIMITATIONS

- Context estimate is chars/4 (documented approximation, budgeting only).
- Single `MAX_CONTEXT_TOKENS` for all providers (no per-provider table yet).
- `model.token` events persist to the events table (uniform replay/fanout at
  the cost of sequence burn on long streams).
- Telemetry in-memory counters are process-local (no cross-worker aggregation).
- QA sandbox stages top-level `*.py` only (packages under test not visible).
- Plugin execute-risk handlers must speak the `handle(args)->dict` protocol
  (print-free stdout; last line parsed as JSON).
- A2A is single-provider, HTTP-only, pre-shared-secret (no PKI/rotation).
- CI's web job installs Chromium per run (~1 min); no self-hosted runner yet.

## BLOCKED

None. All 11 tasks shipped with tests; no task was left red or stubbed.

## Contrast audit (WCAG AA, computed pairs)

| Theme | Pair (text on bg) | Ratio | Verdict |
|-------|-------------------|-------|---------|
| light | #0f172a on #ffffff | 17.85 | AA |
| light | #475569 on #ffffff | 7.58 | AA |
| light | #1d4ed8 on #ffffff | 6.70 | AA |
| light | #047857 on #ffffff | 5.48 | AA |
| light | #92400e on #ffffff | 7.09 | AA |
| light | #b91c1c on #ffffff | 6.47 | AA |
| light | #0369a1 on #ffffff | 5.93 | AA |
| dark | #f1f5f9 on #0f172a | 16.30 | AA |
| dark | #cbd5e1 on #0f172a | 12.02 | AA |
| dark | #93c5fd on #0f172a | 9.90 | AA |
| dark | #34d399 on #0f172a | 9.29 | AA |
| dark | #fbbf24 on #0f172a | 10.69 | AA |
| dark | #f87171 on #0f172a | 6.45 | AA |
| dark | #38bdf8 on #0f172a | 8.33 | AA |
| both | #ffffff on #dc2626 (badge) | 4.83 | AA |

Button variants reuse the same pairs (dark-theme bright tokens use
`#0f172a` text: 6.45–10.69). All ≥ 4.5:1 — no failures to fix.

## Files added / changed (hardening pass)

Added: `services/backup.py`, `services/tool_plugins.py`, `services/a2a.py`,
`api/v1/a2a.py`, `infra/telemetry.py`, `cli/backup.py`, `cli/tools.py`,
`components/ThemeToggle.tsx`, `tools_plugins/_examples/text_stats/`,
`.github/workflows/ci.yml`, 8 test modules, this report.
Changed: `agent_loop.py` (sanitize already landed; +compaction, +metrics),
`memory.py`, `model_router.py` (breaker, streaming, shared recorder),
`providers.py` (SSE), `tools.py` (plugin merge), `config.py` (+14 settings),
`cli/settings.py` (catalog), `cli/chat.py` (token rendering),
`cli/main.py` (groups), `agents/react_agent.py` (budgets, streaming),
`agents/qa.py` (sandbox staging fix), `worker.py` (durations),
`api/main.py` (telemetry, scheduler, A2A state), `api/v1/router.py`
(approval latency), `permissions.py` (`a2a:delegate` marker),
`pyproject.toml` (`memory`/`telemetry` extras), `web` (tokens, theme,
drawer, chat stream, smoke), `docs/` (guide §8/App.A-B, OPERATIONS,
DECISIONS, this report).
