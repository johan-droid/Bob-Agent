# Bob Agent — Audit Verification & Baseline Evidence

**Generated:** 2026-09-18
**Repo:** `https://github.com/johan-droid/Bob-Agent.git`
**Branch:** `main`
**Origin `HEAD` at start of work:** `dd0b606` ("fixes")
**Local `HEAD` at evidence time:** `4594ac4`

This document records, for each claim in the security/readiness audit, what
was actually found in the repository (verified vs. contradicted), the fix
applied, and the fresh phase-0 baseline evidence produced.

---

## 1. Claim-by-claim verification

| Audit claim | Verdict | What was actually found |
|---|---|---|
| Uncommitted in-progress work existed | **CONFIRMED** | Untracked `identity.py`, `outbox.py`, migration `f8a9b0c1d2e3…`, `.gitignore` (+ `uv.lock` out of sync with `pyproject.toml`). |
| `execute_with_policy` builds `PolicyContext` / derives scope **before** validating arguments | **CONFIRMED** | `execution.py`: `_build_policy_context` ran before `validate_arguments`; `execute_request_with_policy` never validated before `_invoke_handler`. |
| `scope_for` fails safe on unknown tool | **CONFIRMED (good)** | `registry.py:116` default-deny on unknown scope; even when raised, was pre-fix a crash surface for malformed args. |
| README install URLs are placeholders | **CONFIRMED — worse than claimed** | `github.com/your-org/bob-agent`, `install.bob-agent.dev`, `brew tap your-org/bob-agent`, `yay -S bob-agent`, plus `github.com/bob-agent/bob-agent.git` inside `bootstrap.py`, `install.sh`, `install.ps1` (wrong upstream org), and `YOUR-HOST` one-liner comments. |
| README says Redis is required; docs elsewhere say optional (drift) | **CONFIRMED** | Top-level README listed Redis as a hard prerequisite; `DEVELOPER_GUIDE`, `STACK.md`, `ARCHITECTURE.md` describe Redis as queue/coordination only ("when present", "optional Docker services"). |
| CI had "no workflow runs / no status" | **CONTRADICTED** | GitHub API shows 5 check-runs on `dd0b606`, **all `failure`** (run `35263240085`, 2026-09-17T19:10:02Z): web, backend tests, migrations, backend security, backend static. Root causes below. |

## 2. Why `dd0b606` CI failed (5/5 jobs, all red)

- Committed code referenced services (`identity`, `outbox`) that were still
  untracked → import errors in backend jobs.
- Migration (`f8a9b0c1d2e3`) did not match ORM models (`ondelete` FK clause,
  spurious `ix_users_created_at` index) → migrations job failed.
- Static job: 20 ruff errors (B008 + E501), 7 mypy errors in `router.py` /
  `identity.py`.
- Web smoke job was not self-contained: it relied on a never-migrated DB, an
  invalid bootstrap secret, and a real (non-echo) provider → could never pass
  in CI. A fresh checkout boots to an empty `agent_system.db` because the app
  does not self-migrate.

No job failed for environmental reasons; every failure was a real repo defect.

## 3. Fixes applied (commit by commit)

| Commit | Scope |
|---|---|
| `13c0e1f` | **Owned the uncommitted work**: committed `identity.py`, `outbox.py`, migration `f8a9b0c1d2e3`, `.gitignore`. |
| `3b29869` | `uv.lock` synced with `pyproject.toml` (added `profilis==1.0.0`). |
| `14f6e9f` | **Repaired baseline gates**: migration model-parity (`ondelete="CASCADE"`, removed `ix_users_created_at`); ruff B008 → `Annotated[Any \| None, Depends(get_principal)]` × 14; E501 wraps; `list_approvals` param reorder; `.value` mypy fixes; `identity.py` ROLE_POWER `frozenset`; `ruff format`; **self-contained `web/smoke-test.mjs`** (scratch-DB migrate, non-default bootstrap secret, forced `DEFAULT_PROVIDER=echo`, `DEFAULT_MODEL=""`). |
| `0b28c98` | **INV-002 validation-before-policy** for `execute_with_policy` (deterministic `ToolValidationError`) and `execute_request_with_policy` (refused `ExecutionResult`, `ExecutionDecision.invalid`, `error="invalid_arguments"`), validation strictly before policy eval / scope derivation / handler. **+36 adversarial contract tests** (16 malformed payload shapes × 2 entry points with spy engine + spy scope, plus valid read/write/approval regression pins). |
| `4594ac4` | **Install/doc contradictions**: all placeholder URLs → `johan-droid/Bob-Agent` (README, OPERATIONS.md, bootstrap.py/install.sh/install.ps1); README package-manager/Homebrew/.exe/`install.bob-agent.dev` options that don't exist repointed at the real one-line installers that do; Redis re-scoped as optional (worker-only). |
| `cc7e7fe` | **This evidence doc** (`AUDIT_VERIFICATION.md`) with claim table, baseline table, and remaining-items list. Pushed by owner → `origin/main`. |
| `2b23563` | **CI-only fixes** found by re-running the real GitHub Actions on `cc7e7fe` (run `35318552017`, 4/5 jobs failing): (1) `mypy src` failed on a pristine checkout because the optional extras (`sentence_transformers`, `opentelemetry.*`) are absent in CI → `[[tool.mypy.overrides]] ignore_missing_imports` for those modules in `pyproject.toml`; (2) web smoke failed strict-mode because `ThemeToggle` renders in both `Header.tsx` and `Sidebar.tsx` → `.first()` in `smoke-test.mjs`. Both verified in a pristine CI-like clone. |
| `aa5b917` | **P0#1 — Telegram durable retry**: `handle_update` persisted the ledger row before processing and skipped ANY redelivery, so a crash between ingest and completion silently dropped the user's request (spec §3/§30 at-most-once). Now the row is marked COMPLETED (`processed_at`) only after processing succeeds; a redelivery whose row is `processed_at IS NULL` is re-processed (at-least-once work, at-most-once completion marker). Regression tests: ingest→crash→redeliver→processed exactly once; completed duplicates stay skipped. |
| `P0#7/P0#2` (`00b4776`) | **Policy `evaluate()` is now pure**: `PermissionGate.peek()` (read-only; synthesizes non-persisted `pending-*` records via the new pure `_build()`), explicit `PermissionGate.consume()` backed by an atomic conditional `UPDATE ... WHERE consumed=0` in `DbApprovalStore.mark_consumed()`, and `PolicyEngine` materializers (`evaluate_and_authorize`/`evaluate_and_materialize`) that persist durable records and spend ALLOW_ONCE grants only when actually authorizing. `execution.py` request path uses `evaluate_and_materialize` so approval ids stay real/decision-able. Proves P0#7 (no side effects on evaluation) and P0#2 (exactly one `consume()` winner under a 12-thread race, `tests/unit/test_policy_purity.py`). |
| `P0#3` (next commit) | **Object-level isolation**: hoisted the approval ownership gate to the top of `PermissionGate.decide()` (a different identified user is refused even for already-decided records — closing the "first-decision-sticks" leak); the `decide_approval` REST endpoint now refuses a non-owner before the non-PENDING short-circuit reveals the record; the Telegram `/approve`+`/deny` path now forwards `decided_by_user_id` so the gate's ownership check actually fires; Telegram `/cancel`+`/retry` refuse tasks owned by another user. Ownerless objects and UID-less principals keep legacy behaviour. Adversarial matrix in `tests/unit/test_object_isolation.py` (6 tests). |

## 4. Fresh baseline evidence (local, at `4594ac4`)

All gates run against the working tree at `4594ac4` (backends: `uv run …`
inside `agent-system/backend`; web: `npm run …` inside `agent-system/web`):

| Gate | Result |
|---|---|
| `uv run ruff check src tests` | PASS (172 files) |
| `uv run ruff format --check src tests` | PASS |
| `uv run mypy src` (strict, 102 files) | PASS, no issues |
| `uv run pytest -q` | **988 passed** (952 prior + 36 new invariant tests), 81 warnings |
| `uv run alembic upgrade head` + `alembic check` | PASS — no new migration ops |
| Web `tsc --noEmit` | PASS |
| Web `npm run build` | PASS |
| Web smoke (`smoke-test.mjs`) | **23/23 PASS** (+1 SKIP: `playwright-core` not installed) |
| `bootstrap.py` / `install.sh` | `py_compile` / `bash -n` clean |

### Local environment caveats (non-defects)

- Local `agent-system/backend/.env.local` sets `DEFAULT_PROVIDER=openrouter`
  with an invalid key (401). Local-only; CI has no `.env.local` and uses the
  `echo` provider by default (`config.py` `default_provider: str = "echo"`).
- Local dev DB `backend/data/agent_system.db` was migrated to head
  `f8a9b0c1d2e3` (27 tables) to make local boots/smoke work. The file is
  untracked; a fresh checkout still requires `make migrate` (or
  `alembic upgrade head`) before first run.

## 5. Remaining items

1. **Drive CI green on `main`**: CI has, in fact, run — on `cc7e7fe` (run
   `35318552017`) 4/5 jobs failed: (a) `mypy src` (root cause fixed in
   `2b23563`, verified in a pristine clone), (b) web smoke (root cause fixed in
   `2b23563`, verified with a real browser), (c) `docker build qa-sandbox`
   (passes locally — log required), (d) `tests/security` (66 passed in a
   pristine clone — log required). Getting the two unexplained jobs' logs and
   pushing still requires `gh auth login` / `GH_TOKEN` on the owner machine.
2. **Fresh authoritative baseline**: `uv run pytest -q` now reports
   **1007 passed** (988 at `cc7e7fe` + 4 Telegram-retry + 9 policy-purity/
   atomic-consume + 6 object-isolation) with `ruff`, `ruff format`, and strict
   `mypy` all clean.
3. Remaining audit P0s to prove next: concurrency/resource enforcement,
   cloud (re)start recovery, planner semantic validation + risk-preserving
   safe fallback. Documented-but-untouched (product posture, not this
   iteration): NULL-owner records, unauthenticated `GET /tasks`, ownerless
   workspaces/artifacts/events (see `tests/unit/test_object_isolation.py` docstring).