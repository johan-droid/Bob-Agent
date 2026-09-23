# Heroku Deployment Guide — Bob Agent

This guide provides step-by-step instructions for deploying Bob Agent to Heroku-24. Instructions are designed for both phone/dashboard administrators and CLI users.

---

## 1. Heroku App Creation

### Dashboard / Phone
1. Log in to [dashboard.heroku.com](https://dashboard.heroku.com).
2. Tap **New** → **Create new app**.
3. Enter an app name (e.g. `my-bob-agent`) and choose your region (United States or Europe).
4. Tap **Create app**.

### Heroku CLI
```bash
heroku create my-bob-agent --stack heroku-24
```

---

## 2. Correct Buildpack Configuration

Bob backend is pure Python/FastAPI. **Do NOT add the Node.js buildpack** for the backend.

### Dashboard / Phone
1. Go to the **Settings** tab of your app.
2. Scroll to **Buildpacks**.
3. Ensure **heroku/python** is the ONLY buildpack listed. If `heroku/nodejs` is listed, tap **Delete** next to it.

### Heroku CLI
```bash
heroku buildpacks:clear --app my-bob-agent
heroku buildpacks:add heroku/python --app my-bob-agent
```

---

## 3. Correct Deployment / Root-Directory Configuration

Bob can be deployed directly from the repository root or via git subtree.

### Root Deployment (Default)
The repository root contains:
- `Procfile` (two process types only: `web` + `release` — there is no worker process)
- `requirements.txt` (`-e ./agent-system/backend[postgres]`)
- `runtime.txt` (`python-3.12.8`)

Deploy directly from root using Git or GitHub Integration:
- In the **Deploy** tab on the Heroku Dashboard, connect your GitHub repository and select the main branch.

### Subtree Deployment (Backend Only)
If deploying only the `agent-system/backend` directory:
```bash
git subtree push --prefix agent-system/backend heroku main
```

---

## 4. Heroku Postgres Setup

Bob requires PostgreSQL in cloud mode for persistent state (sessions, tasks, events, approvals, memory notes).

### Dashboard / Phone
1. Go to the **Resources** tab.
2. In **Add-ons**, search for **Heroku Postgres**.
3. Select plan **Essential-0** (or higher) and tap **Submit Order Form**.
4. Heroku automatically provisions `DATABASE_URL`.

### Heroku CLI
```bash
heroku addons:create heroku-postgresql:essential-0 --app my-bob-agent
```

---

## 5. Redis Requirement

Bob has **no Redis/RQ/worker code** — there is no `agent_system.worker` module and no `REDIS_URL` consumer outside the sandbox env denylist. The only execution mode is inline:

- **`CLOUD_INLINE_RUN=true` (the only mode)** — tasks run in-process inside the `web` dyno via background threads (`task_runner` + `Orchestrator`), with a 10s recovery sweep for orphans/backlog/outbox. No Redis add-on and no worker dyno needed ($7/mo Basic dyno + $5/mo Postgres).
- Do **not** attach Redis or scale a `worker` process type — the root `Procfile` declares only `web` and `release`, so Heroku will reject `heroku ps:scale worker=1`.

---

## 6. Config Vars

Configure these under **Settings** → **Reveal Config Vars** in the Heroku Dashboard.

### REQUIRED (Production Startup — boot guard refuses to start without these)
- `AGENT_ENV`: `production`
- `API_SESSION_SECRET`: long random string (e.g., `openssl rand -base64 32`) — signs API bearer tokens. **Must differ from `BOB_MASTER_ENCRYPTION_KEY`.**
- `AGENT_BOOTSTRAP_SECRET`: long random string — accepted by `POST /api/v1/auth/token` to mint bearers.
- `BOB_MASTER_ENCRYPTION_KEY`: long random string — dedicated vault KEK for `user_credentials`. Falls back to `API_SESSION_SECRET` (key-reuse) with a warning if unset.
- `AGENT_IDENTITY_MODE`: `telegram` (production guard requires it).
- `TELEGRAM_BOT_TOKEN`: token from `@BotFather`.
- `TELEGRAM_WEBHOOK_SECRET`: long random string, sent as `X-Telegram-Bot-Api-Secret-Token` and validated with `hmac.compare_digest`.
- `TELEGRAM_ALLOWED_USER_IDS`: your numeric Telegram **user** id (comma-separated for multiple). **Empty + telegram mode = nobody can provision (fail-closed).** Get your id from `@userinfobot`. (`TELEGRAM_ALLOWED_CHAT_IDS` is the legacy local-mode chat allowlist, not used for identity-mode provisioning.)
- `DATABASE_URL`: provisioned automatically by the Postgres add-on.
- At least ONE model provider key (e.g. `GROQ_API_KEY`); otherwise the agent runs in echo/offline mode.

### RECOMMENDED (Telegram & Cloud Behavior)
- `CLOUD_INLINE_RUN`: `true` (the only execution mode)
- `CLOUD_VAULT_DB`: `true` (memory notes → Postgres instead of ephemeral dyno disk)
- `HEROKU_JAIL`: `true` (dynos have no Docker daemon; shell runs in the subprocess jail)
- `HEROKU_SHELL_ALLOWLIST`: e.g. `python3,pytest,ruff,git,ls,cat` (empty = any binary, approval-gated)
- `TOOLS_SHELL_MODE`: `sandbox` (never `local` on Heroku — `local` is host RCE behind one env var)
- `TOOLS_REQUIRE_APPROVAL`: `true`
- `TELEGRAM_WEBHOOK_URL`: explicit webhook URL, or `HEROKU_APP_NAME` (resolves to `https://<app>.herokuapp.com/api/v1/telegram/webhook`)
- `API_CORS_ORIGINS`: explicit dashboard origin (never `*` combined with credentials)
- `DEFAULT_PROVIDER`: `groq` (or your chosen provider)

### OPTIONAL PROVIDERS
Configure at least ONE model provider key:
- `GROQ_API_KEY`: Groq API Key
- `GEMINI_API_KEY`: Google Gemini API Key
- `OPENROUTER_API_KEY`: OpenRouter API Key
- `OPENAI_API_KEY`: OpenAI API Key
- `ANTHROPIC_API_KEY`: Anthropic API Key
- `NIM_API_KEY`: NVIDIA NIM API Key
- `OPENCODE_API_KEY`: OpenCode API Key
- `OLLAMA_CLOUD_API_KEY`: Ollama Cloud API Key

---

## 7. Process Types

The repository root `Procfile` declares exactly two process types (`Procfile:1-2`):

- **web**: `PYTHONPATH=agent-system/backend/src python -m uvicorn agent_system.api.main:app --host 0.0.0.0 --port ${PORT:-8000}`
  - Binds to Heroku's assigned `$PORT`.
  - Serves FastAPI endpoints, receives the Telegram webhook, runs the 10s recovery sweep (`recover_orphans` + `sweep_backlog` + gateway recover + outbox drain), and executes tasks in-process. Single uvicorn worker (Basic dynos have 512MB).
- **release**: `cd agent-system/backend && PYTHONPATH=src python -m alembic upgrade head`
  - Runs database migrations automatically on every release before new dynos start (idempotent; a failed migration blocks the deploy, previous dynos keep running).

There is **no `worker` process type and no Redis consumer** — do not scale one.

---

## 8. Migration / Release Behavior

When you push new code to Heroku:
1. Heroku compiles the Python build.
2. The `release` process runs `alembic upgrade head` against Heroku Postgres.
3. Once migrations succeed, Heroku restarts the `web` process with the new release.
4. If migrations fail, the deployment aborts and previous dynos continue running safely.

---

## 9. Telegram Webhook Setup

Bob registers its own webhook on boot: `TelegramService.start()` calls `register_webhook()` when transport is `webhook` (i.e. `TELEGRAM_WEBHOOK_SECRET` is set), using `TELEGRAM_WEBHOOK_URL` or `HEROKU_APP_NAME` to resolve `https://<app>.herokuapp.com/api/v1/telegram/webhook` (`services/telegram.py:232-277`, `config.py:312-325`). No manual step is needed.

Manual fallback / verification (e.g. after rotating the secret):

```bash
curl -X POST https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook \
  -d url=https://<app-name>.herokuapp.com/api/v1/telegram/webhook \
  -d secret_token="<TELEGRAM_WEBHOOK_SECRET>"
curl -s https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getWebhookInfo
```

Telegram sends updates with header `X-Telegram-Bot-Api-Secret-Token`, validated with `hmac.compare_digest` (`api/v1/telegram.py:44-50`); bodies are capped at 256KB and deduped via the `telegram_updates` ledger. Local dev (no webhook secret) uses long-polling instead.

---

## 10. Health Check

Verify your deployment using HTTP requests or your browser:

- **Health Endpoint**: `GET https://<app-name>.herokuapp.com/api/v1/health`
  - Expected Response: `{"status": "ok"}`
- **Readiness Endpoint**: `GET https://<app-name>.herokuapp.com/api/v1/ready`
  - Expected Response: `{"status": "ok", "ready": true, "checks": {"database": true, ...}}` (per-service ok-booleans only — detailed `configured/reachable/last_error` stays behind authenticated `GET /api/v1/ready-dependency-check`)
- **Cloud Doctor** (authenticated, replaces `agentctl doctor` — no CLI needed):
  ```bash
  TOKEN=$(curl -s -X POST https://<app-name>.herokuapp.com/api/v1/auth/token \
    -H 'Content-Type: application/json' \
    -d '{"session_secret":"<AGENT_BOOTSTRAP_SECRET>"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
  curl -s -H "Authorization: Bearer $TOKEN" https://<app-name>.herokuapp.com/api/v1/doctor
  ```
  Shows identity mode, transport, webhook URL state, allowlist count (with fail-closed warning), providers, and DB mode — secrets always masked.

---

## 11. First Deployment Verification

1. Open Telegram and send `/start` to your bot **in a DM** (credential setup is DM-only; use your allowlisted user id from `@userinfobot`).
2. Bob will create a session, execute the task, and reply in Telegram. Plain text after `/start` is treated as a goal.
3. Test approval flows for sensitive tools:
   - Use `/approve <approval_id>` or `/deny <id>` directly in Telegram, or tap the inline Approve/Deny buttons.
4. Check `GET /api/v1/doctor` (above) if the bot stays silent: the top causes are an empty `TELEGRAM_ALLOWED_USER_IDS` (fail-closed — nobody can provision), a missing webhook URL, or no provider keys (echo/offline mode).

---

## 12. Restart & Recovery Procedure

If a dyno restarts or cycles:
1. All persistent task state, approvals, events, and Telegram chat mappings remain safe in Heroku Postgres.
2. On startup, the `web` process automatically recovers any orphaned or queued tasks.
3. The outbox delivery loop drains any pending Telegram messages.
4. To manually trigger a restart from Heroku Dashboard: tap **More** → **Restart all dynos**.

---

## 13. Known Ephemeral-Filesystem Limitations

Heroku dynos use ephemeral disk storage:
- **Workspace files, local outputs, and recordings**: Files generated during task execution in dyno `/tmp` or workspace folders are deleted on dyno restart.
- **Durable State**: Task records, memory notes (`CLOUD_VAULT_DB=true`), Telegram identity mappings, and delivery outbox are stored durably in Heroku Postgres and survive restarts.
- **SQLite Backups**: Local `agentctl backup` is disabled in cloud mode; use Heroku Postgres automated daily backups instead (`heroku pg:backups:schedule`).
