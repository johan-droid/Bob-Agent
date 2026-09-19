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
- `Procfile` (points uvicorn, worker, and alembic at `agent-system/backend`)
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

### Cloud Single-Dyno Mode (`CLOUD_INLINE_RUN=true`) — Recommended
- **Redis is NOT REQUIRED.**
- In this mode, tasks fire in-process inside the `web` process via background threads.
- No Redis add-on or worker dyno is needed, keeping costs low ($7/mo Basic dyno + $5/mo Postgres).

### Scaled Worker Mode (`CLOUD_INLINE_RUN=false`)
- **Redis IS REQUIRED.**
- In this mode, tasks are enqueued to RQ and processed by the `worker` process.
- Attach a Redis add-on (e.g. `heroku-redis:mini`) so Heroku sets `REDIS_URL`.

---

## 6. Config Vars

Configure these under **Settings** → **Reveal Config Vars** in the Heroku Dashboard.

### REQUIRED (Production Startup)
- `AGENT_ENV`: `production`
- `API_SESSION_SECRET`: Set to a long random secret string (e.g., `openssl rand -base64 32`)
- `AGENT_BOOTSTRAP_SECRET`: Set to a long random secret string (e.g., `openssl rand -base64 32`)

### RECOMMENDED (Telegram & Cloud Behavior)
- `CLOUD_INLINE_RUN`: `true`
- `CLOUD_VAULT_DB`: `true`
- `HEROKU_JAIL`: `true`
- `TOOLS_SHELL_MODE`: `local`
- `TOOLS_REQUIRE_APPROVAL`: `true`
- `AGENT_IDENTITY_MODE`: `telegram`
- `TELEGRAM_BOT_TOKEN`: Token from Telegram `@BotFather`
- `TELEGRAM_WEBHOOK_SECRET`: Long random secret string
- `TELEGRAM_ALLOWED_CHAT_IDS`: Your numerical Telegram chat ID (comma-separated for multiple)
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

The repository `Procfile` declares three process types:

- **web**: `PYTHONPATH=agent-system/backend/src python -m uvicorn agent_system.api.main:app --host 0.0.0.0 --port ${PORT:-8000}`
  - Binds to Heroku's assigned `$PORT`.
  - Serves FastAPI endpoints, handles Telegram webhooks, and drives sessions in-process when `CLOUD_INLINE_RUN=true`.
- **worker**: `PYTHONPATH=agent-system/backend/src python -m agent_system.worker`
  - Runs Bob's RQ worker process when `CLOUD_INLINE_RUN=false`. Scale to 0 when using inline mode.
- **release**: `PYTHONPATH=agent-system/backend/src python -m alembic -c agent-system/backend/alembic.ini upgrade head`
  - Runs database migrations automatically on every release before new dynos start.

---

## 8. Migration / Release Behavior

When you push new code to Heroku:
1. Heroku compiles the Python build.
2. The `release` process runs `alembic upgrade head` against Heroku Postgres.
3. Once migrations succeed, Heroku restarts the `web` process with the new release.
4. If migrations fail, the deployment aborts and previous dynos continue running safely.

---

## 9. Telegram Webhook Setup

Once your app is deployed and live on Heroku (`https://<app-name>.herokuapp.com`), register your webhook with Telegram:

```bash
curl -X POST https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook \
  -d url=https://<app-name>.herokuapp.com/api/v1/telegram/webhook \
  -d secret_token="<TELEGRAM_WEBHOOK_SECRET>"
```

Telegram will send webhook updates with header `X-Telegram-Bot-Api-Secret-Token`, which Bob validates against `TELEGRAM_WEBHOOK_SECRET`.

---

## 10. Health Check

Verify your deployment using HTTP requests or your browser:

- **Health Endpoint**: `GET https://<app-name>.herokuapp.com/api/v1/health`
  - Expected Response: `{"status": "ok"}`
- **Readiness Endpoint**: `GET https://<app-name>.herokuapp.com/api/v1/ready`
  - Expected Response: `{"status": "ok", "checks": {"database": true}}`

---

## 11. First Deployment Verification

1. Open Telegram and send a message to your bot:
   ```
   Hello Bob, tell me what tools you have available.
   ```
2. Bob will create a session, execute the task, and reply in Telegram.
3. Test approval flows for sensitive tools:
   - Use `/approve <approval_id>` or `/retry <task_id>` directly in Telegram.

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
