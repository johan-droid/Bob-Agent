# Known Issues

Measured, not aspirational. Everything here is reproducible with the commands shown.

**Last verified:** 2026-09-28 · `pytest tests/unit tests/contract tests/security` → **0 failed, 1241 passed,
6 skipped** (~3.5 min, no network). Lint and format clean, mypy clean on all touched files.

## Failing tests: none

The suite is green. The 7 long-standing failures were all environmental rather than
logical, and fixing them surfaced two real production bugs (see below).

## Fixed during the 2026-09-28 audit

### The test suite was not hermetic

A full run made **288 real outbound TCP connections** — `api.telegram.org`,
`api.openrouter.ai`, `api.github.com` — using the developer's real credentials from
`backend/.env.local`, because `Settings` loads that file and `TestClient(app)` runs the app
lifespan (which starts the Telegram poller).

Consequences: the suite consumed the real bot's pending updates, raced a locally running
bot (Telegram 409), burned real LLM credits, and was **green only when the network failed**.
`test_planning_api.py` asserted `strategy.startswith("deterministic")`, which held only
because the real OpenRouter call raised and the deterministic fallback engaged.

Fixed in `tests/conftest.py`: a session-scoped autouse fixture blanks every provider/Telegram
credential and forces `PLANNER_USE_LLM=false`, `VERIFIER_USE_LLM_JUDGE=false`,
`DEFAULT_PROVIDER=echo`. Suite runtime dropped 5:48 → 3:59 and the planning tests now pass
deterministically.

### Process-wide singletons were never reset

`_policy_engine`, `INFERENCE_LOCKS`, `INFERENCE_HEALTH` and `GLOBAL_HEALTH_TRACKER` are
module-level and outlive a test. A rate limit recorded by one test silently removed a
provider from every later test's candidate chain (`model_router._is_routable`). Only 2 of 114
test files reset the policy engine; nothing reset the health tracker at all. Fixed with an
autouse `_reset_singletons` fixture in `conftest.py`.

## Non-blocking, worth fixing (none of these are regressions)

- `unit/test_policy_engine.py::test_approval_existing_grant_allows` has no assertion — it
  discards the `PolicyDecision` and would pass if the engine returned `DENY`.
- `unit/test_classifier.py` has a single assertion that passes identically with
  `_LIVE_DATA_PATTERNS` deleted. The live-data ordering rule (checked *before* the
  `^what\s+(is|are)` chat patterns) is untested.
- `unit/test_telegram_service.py` has 4 tests with no assertions; 2 make real calls to
  `api.telegram.org` with a fake token.
- The forced-final-answer path in `services/agent_loop.py` executes but nothing asserts
  `result.output` on the `max_iters` branch.
- `inference_runtime.invoke(tools=...)` has no test asserting the payload reaches
  `router.invoke`, and `providers._usage` has no test for `prompt_tokens_details: null`
  (the NIM crash this fixed).

## Security findings: all fixed 2026-09-28

Every finding below is closed with a regression test in
`tests/unit/test_audit_security_regressions.py` unless noted.

1. **`file_search` bypassed the secret denylist and the path jail** (FIXED). `_iter_files`
   validated only the search *root*, so `rglob` followed symlinks out of the allowed roots and
   swept up `.env` / `id_rsa` that `file_read` correctly refuses — one read-tier,
   approval-free call exfiltrated any key. It now resolves every hit, re-checks containment
   and `is_secret_path`, and the emitted lines go through `scrub()`.
2. **Sandbox allowlist bypass via interpreter eval flags** (FIXED). The metachar blacklist
   `; | & $ \` < >` allowed quotes, parens and spaces, so
   `python3 -c "__import__('os').popen('id').read()"` passed with `python3` allowlisted, and a
   string-prefix test let `git` authorise `git-evil`. It now matches `argv[0]` exactly and
   refuses `-c/-e/--eval/-m` for any allowlisted program. The metachar check is retained
   because `SubprocessJail` runs `/bin/sh -c`. Ceiling: a config-injection flag
   (`git -c alias.x=!cmd`, `find -exec`) is still not a boundary — documented in a
   `ponytail:` comment, since a string allowlist cannot be made isolation.
3. **MEMBER could self-approve with a client-chosen `ALLOW_ALWAYS`** (FIXED).
   `approval.decide` is granted to MEMBER and the endpoint ignored the caller's role.
   `decide_approval` now requires OWNER/ADMIN, and an ADMIN's `ALLOW_ALWAYS` is downgraded to
   `ALLOW_SESSION` so grant durability cannot exceed the decider's authority.
4. **Vault KEK could fall back to the public default secret** (FIXED for the dangerous
   half). `get_master_key_bytes` refused only when `AGENT_ENV=production`; it now refuses
   outright to derive the key from a default/empty secret, because that encrypted every
   stored credential under a key published in the source. *Still open:* PBKDF2 uses a
   hard-coded, non-per-install salt (`credentials.py::_derive_master_key_bytes`), so all
   deployments sharing a secret derive the same key. Fixing that needs a per-install random
   salt plus a column/migration — deliberately not done unprompted.
5. **Telegram update payloads persisted verbatim, forever** (FIXED). `/setup` collects SSH
   private keys in the message text, which was written to `telegram_updates.payload_json`
   unredacted and copied into every backup. Inbound updates are now redacted on the way in
   (`_redact_update`); the in-memory copy used to process the message is untouched. The
   `scrub_text` PEM pattern also gained an *unterminated* case — a user pasting a key and
   losing the `END` line leaked the whole block.
6. **Browser tools have no SSRF guard** (STILL OPEN, by design of scope). Real Chromium in
   `services/tools/builtin/browser.py` reaches loopback and RFC1918 hosts and follows
   redirects. `_assert_fetchable` exists in the research builtin and is not called here.
   Needs Playwright `page.route()` interception on every request, not just the top-level URL.
7. **Some endpoints lacked ownership checks** (FIXED where real). `get_artifact` now applies
   `enforce_owner_row`. `autopilot_reset` (which re-arms autonomy against shared state) now
   requires OWNER/ADMIN; `autopilot_kill` is deliberately left open because it is a
   fail-safe brake. `delete_scheduled_job` was reported as unguarded but already called
   `enforce_owner_row` — no change needed. `list_recordings` remains unscoped and is left as
   a reported gap.

### Two further production bugs found while fixing the failing tests

- **`SubprocessJail` set `RLIMIT_NPROC` to 64** (FIXED). That limit is per *real UID*, not per
  child, so on any host where the operator already ran more than 64 processes, **every**
  jailed command died with `/bin/sh: 1: Cannot fork` before doing anything. It bought no
  isolation, so it is removed; `RLIMIT_CPU` (which does stop a runaway child) is retained.
- **SSH host verification failed open** (FIXED). The guard read `if verify_host and fp:`, so
  when the fingerprint probe returned `None` — transient network, or a MITM that swallows the
  probe — verification was skipped entirely and the command ran with `host_verified=True`.
  `get_host_fingerprint`'s own docstring says callers must abort on `None`; they now do.

## Documentation rot

The docs tree contains several aspirational documents presented beside as-built ones.
Specifically false, verified 2026-09-28:

- **No Redis, no RQ worker.** `agent-system/docs/CLOUD_HEROKU.md`, `docs/ARCHITECTURE.md`,
  `docs/DEVELOPER_GUIDE.md` §8.2, `docs/OPERATIONS.md` and `EXTERNAL_SERVICES_REPORT.md`
  all describe `agent_system/worker`, `REDIS_URL` and a queue named `agent-system`. None of
  these exist. The `Procfile` declares only `web` and `release`.
- **No `web/` dashboard.** `docs/OPERATIONS.md` documents `make chat`, `make web`,
  `make web-build`, `make web-start` and a 17-route Next.js app. No such Make targets and no
  such directory.
- **No chat REPL.** `docs/CONFIGURATION.md` and `docs/ARCHITECTURE.md` document
  `/tools`, `/model set`, `/memory` slash commands in a `cli/chat.py` REPL that does not
  exist.
- **Event names are fictional.** `docs/DEVELOPER_GUIDE.md` Appendix B lists ~20 events
  (`task.planned`, `run.started`, `approval.decided`, `tool.called`, `batch.*`, `replay.*`…)
  that `domain/events.py` rejects with `UnknownEventTypeError`. The real names are
  `tool.started` / `tool.completed` / `tool.failed`.
- **The planner is not rule-based.** `docs/implementation/DECISIONS.md` (D-09),
  `THREAT_MODEL.md` and `TROUBLESHOOTING.md` all state "no LLM planner exists" and
  `strategy` is always `deterministic_keyword_v1`. `planner.py` has shipped an LLM planner
  (`llm_model_v1`) since.
- **Counts are stale everywhere**: 61→**63** tools, 10→**11** groups, 52→**68** events,
  23→**34** tables, 12→**14** providers, 82→**92** OpenAPI operations, alembic
  `e6f7a8b9c0d1`→**`c4d5e6f7a8b9`**, "605 passed"→**1410 collected**.
- `ssh_execute` and the `ssh` tool group are missing from every capability inventory.
- `documentations/COMPLETE_DOCUMENTATION.md` claims to compile all of `documentations/` but
  omits the only two code-grounded documents in that folder (`31_System_Handbook.md`,
  `32_Ollama_Cloud_Inference_Runtime.md`).
- Two committed `app.json` files disagree on `TOOLS_SHELL_MODE` (`sandbox` at the repo root,
  `local` in `agent-system/backend/`); both docs explicitly forbid `local` on Heroku.

`docs/implementation/ARCHITECTURE_RECONCILIATION.md` does call the `documentations/` product
spec "ASPIRATIONAL BY DESIGN", which is the correct framing — the problem is that the
aspirational files carry no such banner and sit beside the as-built ones.
