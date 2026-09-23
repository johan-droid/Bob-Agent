# Bob Agent — System Handbook (Telegram-only cloud agent)

> Source of truth for the system as implemented. File:line references are
> relative to `agent-system/backend/src/agent_system/`. Supplements (not
> replaces) the build-plan docs in this folder; where they disagree with the
> code, the code wins. Last verified: 2026-09-23.

---

## 1. What the system is

A **Telegram-controlled cloud agent**. The phone is only a Telegram client;
all computation, orchestration, tool execution, approvals, memory, and
recovery run on the server (Heroku `web` dyno + Heroku Postgres):

```
USER (Telegram app) → TELEGRAM → HEROKU WEBHOOK (/api/v1/telegram/webhook)
  → TELEGRAM GATEWAY & IDENTITY (AGENT_IDENTITY_MODE=telegram)
  → ORCHESTRATOR / ReAct LOOP → MODELS + TOOLS + MCP + WEB RESEARCH
  → POSTGRES (sessions, tasks, events, approvals, memory, vault, outbox)
  → TELEGRAM RESPONSE
```

Non-facts (stale docs may say otherwise): there is **no Redis/RQ/worker
process** (no `agent_system.worker` module exists), **no separate dashboard
server**, and the CLI (`agentctl`) is **not in the runtime path** — `api/*`
imports nothing from `cli/*` (verified by grep). The CLI is an optional admin
tool; the server boots and serves from environment variables alone.

---

## 2. Runtime topology

| Piece | Implementation |
|---|---|
| HTTP server | Single uvicorn worker, `api/main.py:lifespan()` builds all services into `app.state` |
| Execution | In-process only (`CLOUD_INLINE_RUN=true` is the only mode): `task_runner` threads + `Orchestrator` |
| Recovery | 10s `recover_tasks` sweep: `recover_orphans` → `sweep_backlog` → gateway `recover` → `Outbox.drain()` (`api/main.py:148-172`) |
| State | Postgres in cloud (`DATABASE_URL`), SQLite locally (`sqlite:///data/agent_system.db`) |
| Files | Ephemeral on dynos — durable state must live in Postgres (`CLOUD_VAULT_DB=true` persists memory notes to `memory_notes`) |
| Processes | Root `Procfile`: `web` (uvicorn) + `release` (`alembic upgrade head`) only |
| Migrations | Alembic, head `a1b2c3d4e5f6` (object ownership + A2A dedup table) |

Boot order in `lifespan()`: telemetry → engine/session factory → event bus →
`PermissionGate` (DB-backed) → `Authenticator` → autopilot (OFF) → Telegram
(start webhook register or polling thread) → skills → soul → model router →
A2A → backup scheduler (SQLite only) → recovery sweep. A `cloud boot ...`
summary line is logged with env/identity/transport/allowlist/providers and
never includes secrets.

---

## 3. Configuration

Settings are pydantic (`config.py:Settings`), merged as
**environ > `.env.local` > `.env` > defaults**. Production guard
(`config.py:289-310`) refuses to boot with dev secrets and requires
`TELEGRAM_BOT_TOKEN` + `TELEGRAM_WEBHOOK_SECRET` + `AGENT_IDENTITY_MODE=telegram`.

| Var | Meaning |
|---|---|
| `AGENT_ENV` | `production` on Heroku |
| `API_SESSION_SECRET` | Signs API bearer tokens. Must differ from the vault KEK |
| `AGENT_BOOTSTRAP_SECRET` | Accepted by `POST /api/v1/auth/token` to mint bearers |
| `BOB_MASTER_ENCRYPTION_KEY` | Dedicated vault KEK (falls back to API secret with a warning if unset) |
| `AGENT_IDENTITY_MODE` | `telegram` (cloud) / `local` (single-operator dev) |
| `TELEGRAM_BOT_TOKEN` | From `@BotFather` |
| `TELEGRAM_WEBHOOK_SECRET` | Validated via `hmac.compare_digest` on `X-Telegram-Bot-Api-Secret-Token` |
| `TELEGRAM_WEBHOOK_URL` / `HEROKU_APP_NAME` | Resolves `.../api/v1/telegram/webhook` |
| `TELEGRAM_ALLOWED_USER_IDS` | Identity-mode allowlist (**user** ids; empty + telegram mode = nobody can provision, fail-closed) |
| `TELEGRAM_ALLOWED_CHAT_IDS` | Legacy local-mode chat allowlist |
| `DATABASE_URL` | Postgres (Heroku) / SQLite (local) |
| `CLOUD_INLINE_RUN` / `CLOUD_VAULT_DB` / `HEROKU_JAIL` | `true` on Heroku |
| `TOOLS_SHELL_MODE` | `sandbox` (never `local` on Heroku) |
| `HEROKU_SHELL_ALLOWLIST` | e.g. `python3,pytest,ruff,git,ls,cat` (empty = any binary, approval-gated) |
| `TOOLS_REQUIRE_APPROVAL` | `true` |
| `DEFAULT_PROVIDER` + `<PROVIDER>_API_KEY` | At least one provider, else echo/offline mode |
| `API_CORS_ORIGINS` | Explicit origin (never `*` with credentials — boot raises) |
| `MCP_SERVERS` | JSON stdio server list (`[]` default) |
| `SCHEDULER_ENABLED` | Nightly SQLite backup (Postgres uses managed backups) |

---

## 4. Telegram gateway

`services/telegram.py` (`TelegramService`), transport from
`telegram_webhook_secret`: set → `webhook`, unset → `polling` (local dev).

- **Webhook auto-registration**: `start()` → `register_webhook()` posts
  `setWebhook` with `secret_token` on every boot; no manual step needed.
- **Validation**: secret header via `compare_digest` (`api/v1/telegram.py:44-50`);
  bodies capped at 256KB; duplicates deduped by the `telegram_updates` ledger
  (`_log_update`, at-least-once with crash-window reclaim).
- **Inbound caps**: message text truncated to 4096 chars server-side.
- **Authorization**: identity mode resolves `from.id` → `IdentityService`
  (auto-provision if allowlisted, first user becomes `owner`); local mode
  checks chat allowlist **plus** sender allowlist in group/supergroup/channel
  chats (group members can't ride a shared chat id).
- **DM-only secrets**: `/setup`/`/rotate` and active setup steps are refused
  in groups; setup sessions are keyed by chat **and** bound to the starting
  user's `user_id`, so a group member can't continue another user's
  `ask_api_key`/`ask_key` step.
- **Commands**: `/start /help /status /cancel /retry /approve /deny /setup
  /connections /test /rotate /revoke /remove` plus inline Approve/Deny
  buttons on approval prompts (`requested_action`/`scope` interpolated
  verbatim but no `parse_mode` is set, so formatting injection is inert).
- **Delivery**: all sends go persist-first through the outbox (see §11);
  empty/whitespace messages are never enqueued (Telegram 400s otherwise).

---

## 5. Identity & auth

Modes (`services/identity.py`): `local` → single `OPERATOR`; `telegram` →
Telegram users provisioned as Bob users (allowlist-gated, blocked stays
blocked, existing accounts idempotent). `allowed_user_ids()` uses **only**
`TELEGRAM_ALLOWED_USER_IDS` — never chat ids, never allow-all on empty.

Bearers (`services/auth.py`): HMAC-signed random tokens, constant-time
verify, accepted via `Authorization: Bearer` or `agent_session` cookie.

- `POST /api/v1/auth/token {session_secret}` → legacy shared bearer
  (rate-limited 10/min/IP).
- `POST /api/v1/auth/token/user {session_secret, telegram_user_id}` →
  **user-bound** bearer (`u_…`, embeds the Bob `user_id`).
- `get_principal()` (`api/deps.py`): user-bound tokens load the owner
  directly and **reject** a mismatched `?principal=` with 403; legacy shared
  bearers use `?principal=<telegram_user_id>` resolved server-side (deprecated,
  warning-logged — migrate to user-bound tokens).
- Ownership helpers: `enforce_session_visible` / `enforce_task_visible`
  (404, never 403, to avoid existence oracles), `apply_owner_filter`,
  `enforce_owner_row`, transitive task/session checks.

---

## 6. API surface (all under `/api/v1`, bearer auth unless noted)

| Group | Endpoints | Notes |
|---|---|---|
| Health | `GET /health`, `GET /ready` (open) | `ready` → `{status, ready, checks}` boolmap only; details behind auth `GET /ready-dependency-check` |
| Doctor | `GET /doctor` (auth) | Cloud `agentctl doctor`: env/identity/transport/allowlist/providers, secrets masked |
| Auth | `POST /auth/token`, `POST /auth/token/user` | Rate-limited; bootstrap-secret gated |
| Sessions | `POST/GET /sessions`, `GET/PATCH/DELETE /sessions/{id}`, `POST /sessions/{id}/plan` | Owner-scoped; goal ≤10k; creates capped 60/min/IP |
| Tasks | `POST/GET /tasks`, `GET /tasks/{id}`, `POST /tasks/{id}/{transition,run,retry}` | `GET /tasks` requires auth + owner filter; idempotency keys owner-scoped; `run`/`retry` use conditional claims (409 on concurrent move); states: `PENDING PLANNING QUEUED RUNNING BLOCKED_APPROVAL RECOVERING REVIEW SUCCEEDED FAILED CANCELLED` (`domain/tasks.py`) |
| Approvals | `GET /approvals`, `POST /approvals`, `POST /approvals/{id}/decision` | Legacy NULL-owner rows hidden in telegram mode; first-decision-sticks is atomic |
| Workspaces | CRUD + `tree/file/fingerprint/exec` | Owner-gated; `exec` requires approval gate + forces `network=False` |
| Artifacts / Events | `GET /artifacts`, `GET /events` | Artifacts owner + task-transitive; events session-enforced + owned post-filter |
| Vault | `GET /vault/notes`, `GET /vault/note`, `POST /vault/notes`, `DELETE /vault/notes` | File-vault owner stamp in frontmatter; telegram mode hides/deletes only own notes; traversal via `is_relative_to`; body ≤100KB |
| Templates | `GET/POST /templates`, `POST /templates/{id}/restore`, `DELETE` | Owner stored in meta JSON; workspace ownership verified |
| Skills | `/skills*` incl. `POST /skills/import` | URL import: https-only, host blocklist, Content-Type + 2MB cap; git clone with `--` + `-` rejection |
| Settings | `GET /settings`, `GET/POST /settings/{key}` | Via `services/settings_store.py` (no CLI import); secrets always masked over API |
| Realtime | `WS /ws/events`, `GET /events/stream`, `GET /events/latest-sequence` | Telegram mode streams only owned sessions; replay capped at 200; WS handshake throttled 20/min/IP; `?token=` works but is proxy-logged |
| A2A | `POST /a2a/delegate` (auth), `POST /a2a/callback` (HMAC, no bearer), `GET /a2a/delegations` | `issued_at` required; envelope-hash dedup table; completion events only on real `RUNNING→SUCCEEDED`; callback body capped 256KB |
| Router | `/router/catalog|roles|preview` | Model catalog |

Global input guards: `TaskCreate.input/depends_on` capped, `Recipe.task_dag` ≤100KB, schedule payload ≤10KB each, Telegram text ≤4096, webhook/callback bodies ≤256KB.

---

## 7. Task lifecycle & execution

1. Goal → session → `Planner` (LLM decomposition with deterministic fallback)
   → persisted `PENDING` tasks (planning is one-shot; 409 if tasks exist).
2. Explicit `POST /tasks/{id}/run` moves `PENDING/FAILED → QUEUED`
   (conditional claim) and kicks the in-process runner (never blocks; progress
   via events).
3. Runner executes the ReAct loop (`agent_loop` + `react_agent`): model call →
   tool calls (each validated + authorized) → compaction at 75% of
   `max_context_tokens` → result. Verifier gate (`verifier_enabled`) walks
   `RUNNING → REVIEW → SUCCEEDED/FAILED` unless lenient-pass applies.
4. Terminal: `SUCCEEDED/FAILED/CANCELLED`. Only `FAILED → QUEUED/RECOVERING`
   via explicit retry (conditional claim in both REST and `cloud.retry_task_queued`).
5. Crash recovery: `recover_orphans` + `sweep_backlog` re-queue expired-lease
   work; gateway `recover` + outbox `drain` redeliver Telegram messages.

Budgets: `daily_budget_usd` / `max_task_cost_usd` via `BudgetLedger` +
durable `model_calls` cost rows; resource caps (`max_*`) are config, with
`RLIMIT_AS/CPU/NPROC/FSIZE` enforced in the jail child.

---

## 8. Permissions & approvals

One gate: `services/permissions.py:PermissionGate` (DB-backed `ApprovalStore`;
durable across restarts). Tiers `LOW/MEDIUM/HIGH/CRITICAL`, policies
`ALLOW_ONCE/ALLOW_SESSION/ALLOW_WORKSPACE/ALLOW_ALWAYS/DENY`, TTLs 60/30/15/5
min, expired = deny.

- **Default-deny scopes** (never `ALLOW_ALWAYS`): `host:filesystem`,
  `host:shell`, `host:credentials`, `host:users`, `host:firewall`,
  `host:bootloader`, `host:security_software`, `browser:transact`,
  `credential:transmit`, `autopilot:input`, `a2a:delegate`.
- **Atomicity**: `decide()` uses conditional `UPDATE … WHERE
  decision='PENDING'` (first-decision-sticks under concurrent approve/deny);
  `check()` consumes `ALLOW_ONCE` via atomic `mark_consumed()` (one winner)
  and never matches another owner's grant (owner + session/workspace binding).
- **Tool scopes**: `shell:<cmd60>`; `coding:run_tests|lint|…` bind explicit
  `command` overrides to `coding:{name}:{sha16}` so one approval can't cover
  arbitrary commands; `research_*` are read-tier (no approval) with SSRF
  guards (metadata/loopback/private hosts, non-80/443 ports, same-origin
  redirects only).
- Tools declare `risk` (`read/write/execute/destructive`); `Tool.scope_for(args)`
  supports callables; `execute_tool()` is the single validate → authorize →
  invoke funnel. `PolicyEngine` singleton caveat is fixed (no stale factory).

---

## 9. Sandbox, tools, MCP, browser

- **Docker** (`DockerSandbox`): `cap_drop=ALL`, no-new-privileges, private
  IPC, noexec `/tmp`; network disabled unless explicitly requested (this
  endpoint never grants it).
- **Heroku jail** (`SubprocessJail`, `HEROKU_JAIL=true`): rlimits, scrubbed
  env, timeouts, output caps, allowlist with metachar rejection
  (`;|&$\`><newline` rejected when an allowlist is set). **Inherits host
  network** — `network=False` is containment, not isolation (logged warning).
- **MCP stdio**: spawned with scrubbed env + per-server overlay (never full
  `os.environ`); argv without shell; body/line caps; content-type allowlist.
- **File jail** (`tools/paths.py`): roots are `workspaces/` + `outputs/` (+
  `tools_fs_roots`) — never bare repo root; symlinks resolved pre-check;
  secret basenames refused.
- **Browser**: max 3 sessions, 30-min TTL with eviction, output capped.
- **Prompt-injection posture**: system prompt marks tool/web/file/memory/history
  output **untrusted data**; memory/history injected under explicit
  `--- Untrusted … ---` delimiters and secret-scrubbed; only ```` ```tool: ````
  fences were ever structural (social-engineering text still needs an approval
  to execute anything).

---

## 10. Realtime, outbox, A2A

- **Realtime** (`api/v1/realtime.py`): WS + SSE, replay-then-follow,
  heartbeat 15s, sequence-resumable. Telegram principals receive only owned
  sessions (task→session transitive); replay capped at 200/connect.
- **Outbox** (`services/outbox.py`): persist-first Telegram delivery.
  Claim sets `state→SENDING` with `next_attempt_at<=now` predicate +
  `claimed_by` worker id (`FOR UPDATE SKIP LOCKED` on Postgres);
  `_mark_delivered` accepts `PENDING/RETRY/SENDING`; `reap_stuck` covers all
  claimed states; exponential backoff to `outbox_max_attempts` then `DEAD`.
  Empty texts are never enqueued and legacy empties are marked
  delivered-skipped (Telegram 400s otherwise).
- **A2A** (`services/a2a.py`): HMAC-signed envelopes (`issued_at` required,
  freshness window); per-target approvals (`a2a:delegate:*`); callback dedup
  by envelope SHA256 in `a2a_processed_envelopes` (replays return
  `status:duplicate`); `task.completed` emitted only on a real transition
  (no phantom completions).

---

## 11. Persistence & crypto

Postgres tables: `sessions`, `tasks` (both `owner_user_id`), `approvals`
(owner-gated decide/list/consume), `events` (session/task links),
`model_calls`, `tool_calls`, `workspaces`, `artifacts`, `workspace_templates`,
`task_batches`, `qa_reports`, `recipes`, `insights`, `scheduled_jobs`,
`behavior_recordings`, `memory_notes`, `user_credentials` (envelope-encrypted),
`telegram_*` (accounts, updates ledger, gateway messages, chat history),
`delivery_outbox`, `a2a_processed_envelopes`, `worker_attempts`,
`swarm_members`, plus cost/scheduler rows. Migration head:
`alembic/versions/a1b2c3d4e5f6_object_ownership_a2a_dedup.py` backfills the
`owner_user_id` columns and the dedup table. No Postgres RLS — isolation is
app-layer (`api/deps.py` + per-endpoint owner filters); every missed filter is
a cross-tenant read, so new endpoints must copy the pattern.

Crypto: per-credential DEK (AES-256-GCM) wrapped by a KEK derived via
PBKDF2-SHA256/100k from `BOB_MASTER_ENCRYPTION_KEY` (warns + falls back to
the API secret if unset — set it). Legacy Fernet rows still decrypt (migrate
then remove). Backups: SQLite copy + vault/recordings tar with secret basenames
excluded, `chmod 0600`/`0700`; Postgres deployments rely on managed backups
(SQLite scheduler never runs there).

Memory: `DbNoteStore.recall(owner=None)` returns only legacy NULL-owner rows
(fail-closed); file vault stamps `owner_user_id` in frontmatter on create and
hides/deletes foreign notes in telegram mode; note bodies/titles scrubbed on
write with a byte cap.

---

## 12. Observability & error handling

- `GET /health` (`{"status":"ok"}`), `GET /ready`
  (`{status, ready, checks}` boolmap), `GET /doctor` (auth, §6).
- Approval latency/cost metrics in-process; `record_cost` evicts beyond 1000
  sessions and emits no per-session OTel label (cardinality guard).
- Secret redaction: `services/secrets.py` (`redact_dict/value`, key markers +
  value patterns incl. `gsk_`, `oc_sk_`, Telegram tokens,
  Bearer); provider `last_error` redacted before storage; 5xx responses are
  generic (`internal error`), 4xx carry safe messages.
- Logs: user-controlled strings interpolated as values; `session_id` never a
  metric label. API docs (`/docs`) disabled in production; CORS rejects
  `*`+credentials and narrows methods/headers.

---

## 13. Security model (summary)

| Threat | Control |
|---|---|
| Webhook forgery | Secret header + `compare_digest`, 256KB cap, ledger dedup |
| Telegram impersonation | User-id resolution + allowlist fail-closed + group-sender check |
| Bearer theft → lateral movement | Owner-scoped queries everywhere; user-bound tokens (mismatch = 403) |
| Secrets in transit/at rest | Masked settings API, envelope encryption, redacted logs/errors/health |
| Prompt injection → RCE | Untrusted-data prompt rule + delimiters; every execute path approval-gated with command-bound scopes |
| Sandbox escape | Docker hardening / jail rlimits+allowlist; MCP scrubbed env; file jail without repo root |
| SSRF/metadata | Research/browser host/port/redirect guards; skills import https+allowlist+size caps |
| Race/double-spend | Atomic approval decide/consume; conditional task claims; outbox SENDING+SKIP LOCKED; A2A dedup |
| DoS/cost blowup | Body/input caps, rate limits (token 10/min, creates 60/min, WS 20/min), replay cap 200, budgets, browser TTL, backup retention |

Residual risks (accepted, tracked): no Postgres RLS (app-layer only),
Fernet fallback still live, pre-existing file-vault notes lack owner stamps
(hidden fail-closed), `research_*` stay read-tier by design, supply chain
(`open-connector:latest` digest pin pending, Heroku resolves fresh deps —
keep `uv.lock` in sync).

---

## 14. Operations

**Heroku** (see `CLOUD_HEROKU.md` for click-paths): set config vars (§3) →
`git push heroku main` (release migrates, web boots, webhook self-registers)
→ `GET /ready` → `/start` in Telegram DM → `/doctor` on silence. Rotate a
leaked bot token via `@BotFather` → update `TELEGRAM_BOT_TOKEN` → restart.
Postgres PITR/backups via `heroku pg:backups`.

**Local** (no CLI needed): `cd agent-system/backend && uv sync &&
uv run alembic upgrade head && uv run uvicorn agent_system.api.main:app
--port 8000` with env from `.env`/`.env.local` (polling transport when no
webhook secret). Mint a bearer via `/auth/token`, then `/doctor`.

**Tests**: `uv run pytest tests/contract/test_api_v1.py`
(auth/ready/settings), `test_state_machine_hammer`, `test_task_runner`,
`test_telegram_gateway_e2e`, `test_telegram_harness_e2e`,
`test_failure_model` — all green at time of writing.
