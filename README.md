# Bob Agent — Telegram-Only Cloud Agent

## Overview

Bob Agent is a **Telegram-controlled cloud agent** that runs its computation, orchestration, and tool execution in the cloud on **Heroku**. Telegram is the sole user communication and control channel.

The operational architecture is:

```
User → Telegram → Heroku Webhook → Telegram Gateway → Identity & Auth → Orchestrator → Tools/Models/MCP → Outbox → Telegram → User
```

All agent execution occurs in the cloud. The user's phone is simply the Telegram client interface.

## Core Features

- **Telegram Canonical Interface** — Interact with Bob exclusively through Telegram text messages, commands (`/start`, `/help`, `/status`, `/cancel`, `/retry`, `/setup`, `/connections`, `/test`, `/rotate`, `/revoke`, `/remove`), and inline approval buttons.
- **Heroku Cloud Execution** — Executes tasks in-process on the Heroku web dyno (`CLOUD_INLINE_RUN=true`). No separate background worker or Redis instance is required for production.
- **Durable Cloud State** — Persists tasks, state machine transitions, Telegram update ledger, identity mappings, credentials, memory, and outbox messages to PostgreSQL (`DATABASE_URL`). Survives dyno restarts and crash recoveries.
- **Telegram Identity Boundary** — Production operates with `AGENT_IDENTITY_MODE=telegram`. Every request is mapped to a provisioned Bob user with role-based authorization, task ownership, and credential isolation.
- **Production Webhook Integration** — Automatically configures Telegram's webhook (`setWebhook`) at startup using `HEROKU_APP_NAME` or `TELEGRAM_WEBHOOK_URL` with HMAC header validation (`X-Telegram-Bot-Api-Secret-Token`).
- **Rich Capability Ecosystem** — 12+ LLM providers (Groq, Anthropic, OpenAI, Gemini, OpenRouter, etc.), shell subprocess jail (`HEROKU_JAIL=true`), web research, OpenConnector SaaS actions, MCP servers, Obsidian vault notes, and pluggable skills.
- **Approval Gate** — Risky actions pause and send an approval prompt with inline `[Approve]` and `[Deny]` buttons directly to authorized Telegram users.
- **Interactive Setup** — Configure SSH keys, API credentials, and integrations interactively via Telegram using `/setup <provider>`.

## Production Deployment (Heroku)

### Prerequisites

- A [Heroku](https://heroku.com) account and [Heroku CLI](https://devcenter.heroku.com/articles/heroku-cli)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- An API key for an LLM provider (e.g., [Groq](https://console.groq.com) or [OpenAI](https://platform.openai.com))

### Deployment Steps

1. **Clone the Repository:**
   ```bash
   git clone https://github.com/johan-droid/Bob-Agent.git
   cd Bob-Agent
   ```

2. **Create Heroku App & PostgreSQL Addon:**
   ```bash
   heroku apps:create my-bob-agent
   heroku addons:create heroku-postgresql:essential-0 -a my-bob-agent
   ```

3. **Configure Required Environment Variables:**
   ```bash
   heroku config:set -a my-bob-agent \
     AGENT_ENV=production \
     AGENT_IDENTITY_MODE=telegram \
     TELEGRAM_BOT_TOKEN="your-bot-token" \
     TELEGRAM_WEBHOOK_SECRET="$(openssl rand -hex 32)" \
     HEROKU_APP_NAME="my-bob-agent" \
     DEFAULT_PROVIDER="groq" \
     GROQ_API_KEY="your-groq-api-key" \
     HEROKU_JAIL=true \
     CLOUD_INLINE_RUN=true \
     CLOUD_VAULT_DB=true \
     API_SESSION_SECRET="$(openssl rand -hex 32)" \
     AGENT_BOOTSTRAP_SECRET="$(openssl rand -hex 32)"
   ```

4. **Deploy to Heroku:**
   ```bash
   git push heroku main
   ```

5. **Start Operating Bob:**
   Open Telegram, search for your bot, and send `/start`.

## Telegram Commands & Usage

Message your Telegram bot directly to control Bob Agent:

- `/start` — Connect Bob to the chat and receive welcome instructions.
- `/help` — List available control plane commands and syntax.
- `/status` — View active sessions, running tasks, and system status.
- `/cancel <task_id>` — Cancel a running task.
- `/retry <task_id>` — Re-queue a failed task.
- `/approve <approval_id>` — Grant permission for a pending action.
- `/deny <approval_id>` — Refuse permission for a pending action.
- `/setup <provider>` — Interactively configure credentials (e.g., `/setup ssh` or `/setup github`).
- `/connections` — List connected integrations and their status.
- `/test <connection>` — Test connectivity of an integration.
- `/rotate <connection>` — Rotate a stored credential.
- `/revoke <connection>` — Temporarily revoke an integration.
- `/remove <connection>` — Permanently remove a stored credential.

Or simply send a natural language goal, for example:
- `Research quantum computing developments and summarize key papers.`
- `Check my repository and fix the failing tests.`

## Administrative CLI (`agentctl`)

The CLI (`agentctl`) is used solely for development, deployment, database migrations, diagnostics, and administrative maintenance. It is **not** an end-user interface.

```bash
# System diagnostics
uv run agentctl doctor

# Check system status
uv run agentctl status

# List tasks across sessions
uv run agentctl tasks list

# Manage pending approvals
uv run agentctl approvals list

# Database migrations
cd agent-system/backend && uv run alembic upgrade head
```

## Configuration Options

| Variable | Description | Default / Required in Production |
|----------|-------------|----------------------------------|
| `AGENT_ENV` | Environment mode (`dev` or `production`) | `production` |
| `AGENT_IDENTITY_MODE` | Identity mode (`local` or `telegram`) | `telegram` (Required) |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather | **Required** |
| `TELEGRAM_WEBHOOK_SECRET` | Secret token for webhook validation | **Required** |
| `HEROKU_APP_NAME` | Heroku app name for webhook URL | `https://<app_name>.herokuapp.com/api/v1/telegram/webhook` |
| `TELEGRAM_WEBHOOK_URL` | Explicit webhook URL override | Optional |
| `DATABASE_URL` | PostgreSQL connection string | **Required** (Set automatically by Heroku Postgres) |
| `DEFAULT_PROVIDER` | Primary LLM provider | `groq` |
| `CLOUD_INLINE_RUN` | Run task execution in-process | `true` |
| `HEROKU_JAIL` | Enable subprocess isolation jail | `true` |
| `CLOUD_VAULT_DB` | Store memory notes in DB | `true` |

## Project Structure

```
Bob Agent/
├── agent-system/
│   ├── backend/            # FastAPI backend + agent execution engine
│   │   ├── src/agent_system/
│   │   │   ├── agents/     # ReAct agent & definitions
│   │   │   ├── api/        # FastAPI routes & Telegram webhook
│   │   │   ├── cli/        # agentctl administrative commands
│   │   │   ├── domain/     # Tasks, lifecycles, and events
│   │   │   ├── infra/      # ORM models, DB, event bus, outbox
│   │   │   └── services/   # Telegram gateway, orchestrator, tools, credentials
│   │   └── tests/          # Test suite
│   ├── bootstrap/          # Admin bootstrapper
│   └── skills/             # Built-in skills
├── Procfile                # Heroku process definition (web + release)
├── app.json                # Heroku One-Click deploy schema
└── README.md
```

## License

MIT
