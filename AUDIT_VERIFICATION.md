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

1. **Push** the four commits to GitHub and **watch CI**: re-running the 5
   jobs on the new `main` HEAD is the authoritative green evidence. Not done
   here (requires a push; left to the repo owner).
2. Re-run the security/static workflows (bandit/semgrep if configured) on the
   repaired tree — present in workflow but not executed locally.