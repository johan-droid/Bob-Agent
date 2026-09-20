# 🤖 Bob Agent — Telegram-Only Cloud Agent

### Operational Model

```
USER ──► TELEGRAM ──► HEROKU WEBHOOK ──► GATEWAY & IDENTITY ──► AGENT CORE ──► ORCHESTRATOR ──► TOOLS & MODELS ──► POSTGRES OUTBOX ──► TELEGRAM
```

Bob Agent is a **Telegram-controlled cloud agent** running its computation, orchestration, and tool execution on **Heroku**.

Telegram is the sole user interface and control plane. All actual computation, task execution, tool use, model routing, and state persistence occur in the cloud on Heroku.

---

## Capabilities & Architecture

- **Telegram Interface**: All user interactions occur through Telegram messages and commands.
- **Telegram Identity (`AGENT_IDENTITY_MODE=telegram`)**: Resolves Telegram user IDs to Bob user accounts, enforcing ownership, permissions, and object-level isolation.
- **Webhook Production Transport**: Operates via `/api/v1/telegram/webhook` with header secret validation (`X-Telegram-Bot-Api-Secret-Token`). Auto-configures webhook URL on startup.
- **Heroku Cloud Computer**: ReAct agent loops, model calls, shell execution in jail (`HEROKU_JAIL=true`), MCP servers, and background recovery run on Heroku dynos.
- **Durable Postgres State**: Sessions, tasks, task state, update ledger, credentials, approvals, memory, and outbox messages persist in Heroku Postgres across dyno restarts.
- **Inline Task Execution (`CLOUD_INLINE_RUN=true`)**: Tasks execute in-process on the Heroku web dyno via `task_runner` and `Orchestrator`. No separate worker process or Redis is required.
- **Control Commands**: `/start`, `/help`, `/status`, `/cancel`, `/retry`, `/approve`, `/deny`, `/setup`, `/connections`, `/test`, `/rotate`, `/revoke`, `/remove`.

---

## Configuration

Secrets belong in environment variables on Heroku.

### Key Environment Variables

| Variable | Description | Production Value |
|----------|-------------|------------------|
| `AGENT_ENV` | Application environment | `production` |
| `API_SESSION_SECRET` | Session secret key | Random secret string |
| `AGENT_BOOTSTRAP_SECRET` | Bootstrap secret key | Random secret string |
| `AGENT_IDENTITY_MODE` | Identity mode | `telegram` |
| `TELEGRAM_BOT_TOKEN` | Bot API token from `@BotFather` | `123456:ABC...` |
| `TELEGRAM_WEBHOOK_SECRET` | Webhook secret token | Random secret string |
| `HEROKU_APP_NAME` | Heroku application name | `my-bob-agent` |
| `TELEGRAM_WEBHOOK_URL` | Explicit webhook URL (optional) | `https://my-bob-agent.herokuapp.com/api/v1/telegram/webhook` |
| `DATABASE_URL` | Heroku Postgres connection string | Automatically set by Heroku Postgres add-on |
| `CLOUD_INLINE_RUN` | Inline task execution | `true` |
| `HEROKU_JAIL` | Subprocess jail for Heroku | `true` |
| `DEFAULT_PROVIDER` | Default LLM provider | `groq` (or chosen provider) |

---

## Heroku Deployment Quickstart

1. Create Heroku app & add Postgres:
   ```bash
   heroku create my-bob-agent --stack heroku-24
   heroku addons:create heroku-postgresql:essential-0 --app my-bob-agent
   ```

2. Configure environment variables:
   ```bash
   heroku config:set \
     AGENT_ENV=production \
     API_SESSION_SECRET=$(openssl rand -base64 32) \
     AGENT_BOOTSTRAP_SECRET=$(openssl rand -base64 32) \
     AGENT_IDENTITY_MODE=telegram \
     TELEGRAM_BOT_TOKEN="<YOUR_BOT_TOKEN>" \
     TELEGRAM_WEBHOOK_SECRET=$(openssl rand -base64 32) \
     HEROKU_APP_NAME="my-bob-agent" \
     CLOUD_INLINE_RUN=true \
     CLOUD_VAULT_DB=true \
     HEROKU_JAIL=true \
     DEFAULT_PROVIDER=groq \
     GROQ_API_KEY="<YOUR_GROQ_KEY>" \
     --app my-bob-agent
   ```

3. Deploy:
   ```bash
   git push heroku main
   ```

4. Verify on Telegram:
   - Send `/start` to your Telegram bot.

---

## Development & Testing

```bash
cd backend
uv run pytest                          # Run unit & contract test suite
uv run ruff check .                    # Linting
uv run ruff format --check .           # Formatting check
uv run mypy --strict src               # Strict type check
uv run alembic check                   # Database migration check
```

---

## Administrative Maintenance CLI (`agentctl`)

`agentctl` is retained strictly for developer and administrator maintenance:

- `agentctl doctor`: Environment and system diagnosis.
- `agentctl setup`: Interactive setup wizard.
- `agentctl settings`: View or modify configuration keys.
