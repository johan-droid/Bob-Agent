# Bob Agent — Telegram-Only Cloud Agent

Bob Agent is a **Telegram-controlled cloud agent** running its computation, orchestration, and tool execution on **Heroku**.

The product operational model is:

```
USER (Telegram app)
  │
  ▼
TELEGRAM
  │
  ▼
HEROKU WEBHOOK (/api/v1/telegram/webhook)
  │
  ▼
TELEGRAM GATEWAY & IDENTITY (AGENT_IDENTITY_MODE=telegram)
  │
  ▼
BOB AGENT CORE & ORCHESTRATOR
  │
  ▼
MODELS + TOOLS + MCP + WEB RESEARCH + CLOUD EXECUTION
  │
  ▼
DURABLE STATE / OUTBOX (Heroku Postgres)
  │
  ▼
TELEGRAM RESPONSE
```

Telegram is the **sole user communication and control channel**. Computation, model routing, task execution, tool use, approval enforcement, memory operations, and recovery run entirely in the cloud on Heroku. The user's phone is simply the Telegram client.

---

## Capabilities & Architecture

- **Telegram Control Plane** — Operate Bob through Telegram natural-language goals or interactive slash commands (`/start`, `/help`, `/status`, `/cancel`, `/retry`, `/approve`, `/deny`, `/setup`, `/connections`, `/test`, `/rotate`, `/revoke`, `/remove`).
- **Telegram User Identity (`AGENT_IDENTITY_MODE=telegram`)** — Resolves Telegram users to Bob user accounts, enforcing ownership, permissions, and approval boundaries.
- **Heroku Cloud Execution** — All agent computation (ReAct loops, model calls, shell execution in jail, MCP servers, web research, scheduled work, memory, approvals) runs on Heroku.
- **Durable Cloud Persistence** — PostgreSQL stores sessions, tasks, task state, update ledger, memory, credentials, approvals, recovery metadata, and delivery outbox across dyno restarts.
- **Webhook Production Transport** — Automatic Telegram webhook configuration via `TELEGRAM_WEBHOOK_URL` or `HEROKU_APP_NAME` with secret token validation (`X-Telegram-Bot-Api-Secret-Token`).
- **Inline Cloud Execution (`CLOUD_INLINE_RUN=true`)** — Tasks execute in-process on the web dyno. No separate Redis or RQ worker process is required.
- **12+ Model Providers** — Groq, Gemini, OpenRouter, OpenAI, Anthropic, DeepSeek, Together, Mistral, HuggingFace, NIM, OpenCode, and Ollama Cloud.
- **Administrative CLI (`agentctl`)** — Retained exclusively for administrative maintenance, development, migrations, diagnostics (`agentctl doctor`), and credential setup (`agentctl setup`).

---

## Heroku Deployment

### 1. Prerequisites
- A [Heroku](https://heroku.com) account.
- A Telegram bot token from [@BotFather](https://t.me/BotFather).
- At least one model provider API key (e.g. Groq, Gemini, OpenRouter, OpenAI).

### 2. Deployment Steps

1. **Create Heroku App**
   ```bash
   heroku create my-bob-agent --stack heroku-24
   ```

2. **Add Heroku Postgres**
   ```bash
   heroku addons:create heroku-postgresql:essential-0 --app my-bob-agent
   ```

3. **Set Required Environment Variables**
   ```bash
   heroku config:set \
     AGENT_ENV=production \
     API_SESSION_SECRET=$(openssl rand -base64 32) \
     AGENT_BOOTSTRAP_SECRET=$(openssl rand -base64 32) \
     AGENT_IDENTITY_MODE=telegram \
     TELEGRAM_BOT_TOKEN="123456789:ABC-DEF1234ghIkl-zyx57W2v1u123ew11" \
     TELEGRAM_WEBHOOK_SECRET=$(openssl rand -base64 32) \
     HEROKU_APP_NAME="my-bob-agent" \
     CLOUD_INLINE_RUN=true \
     CLOUD_VAULT_DB=true \
     HEROKU_JAIL=true \
     TOOLS_SHELL_MODE=sandbox \
     DEFAULT_PROVIDER=groq \
     GROQ_API_KEY="gsk_..." \
     --app my-bob-agent
   ```

4. **Deploy Application**
   ```bash
   git push heroku main
   ```
   Heroku runs database migrations automatically via the `release` phase in `Procfile` before starting the `web` process.

---

## Telegram Webhook Auto-Configuration

Bob automatically registers its webhook endpoint with Telegram when starting up in webhook mode.

### Webhook URL Resolution
1. Explicit setting: `TELEGRAM_WEBHOOK_URL` (e.g., `https://my-bob-agent.herokuapp.com/api/v1/telegram/webhook`).
2. Automatic Heroku setting: `HEROKU_APP_NAME` (resolves to `https://<HEROKU_APP_NAME>.herokuapp.com/api/v1/telegram/webhook`).

On startup, Bob calls Telegram's `setWebhook` API with the secret token (`TELEGRAM_WEBHOOK_SECRET`). Incoming Telegram updates are validated via the `X-Telegram-Bot-Api-Secret-Token` header.

---

## Operating Bob via Telegram

### Natural Language Goals
Simply text Bob on Telegram:
- *"Research the latest news on Rust 1.85 and summarize it."*
- *"Check this repository and diagnose the bug in main.py."*
- *"Set up an SSH integration for my home server."*

### Telegram Control Commands
| Command | Action |
|---------|--------|
| `/start` | Connect Bob to the chat |
| `/help` | Show available commands and permissions |
| `/status` | Summarize active sessions and task statuses |
| `/cancel <task_id>` | Cancel a running task |
| `/retry <task_id>` | Re-queue a failed task |
| `/approve <id>` | Grant a pending approval request |
| `/deny <id>` | Deny a pending approval request |
| `/setup [provider]` | Step-by-step interactive integration setup |
| `/connections` | List active connected integrations |
| `/test <ref>` | Test connection health (e.g., `/test ssh:vps`) |
| `/rotate <ref>` | Rotate credential for an integration |
| `/revoke <ref>` | Revoke access for an integration |
| `/remove <ref>` | Delete an integration credential |

---

## Smoke-Test Sequence

1. **Verify Health & Readiness**:
   ```bash
   curl -s https://my-bob-agent.herokuapp.com/api/v1/health
   # Expected: {"status": "ok"}

   curl -s https://my-bob-agent.herokuapp.com/api/v1/ready
   # Expected: {"status": "ok", "ready": true, "mode": "inline", ...}
   ```

2. **Verify Telegram Connection**:
   - Send `/start` in Telegram → Bob replies: `"Bob Agent connected. Send a goal or use /help."`

3. **Verify Goal Execution**:
   - Send a goal: `"Research Python 3.12 release notes and summarize."`
   - Bob acknowledges, creates a session, executes tasks in Heroku cloud runtime, and returns the result in Telegram.

4. **Verify Approvals**:
   - For high-risk actions, Bob pushes an approval button prompt to Telegram with `Approve` and `Deny` inline buttons. Tap `Approve` or send `/approve <id>`.

5. **Verify Dyno Restart Recovery**:
   - Restart dynos (`heroku restart`). Send `/status` in Telegram — sessions, tasks, and identity persist in Postgres.

---

## Administrative CLI (`agentctl`)

The CLI is strictly an administrative and diagnostic tool:

```bash
cd agent-system/backend
uv run agentctl doctor       # Probe machine environment & system checks
uv run agentctl setup        # Interactive credential wizard
uv run agentctl settings     # View or edit system settings
```

---

## License

This project is licensed under the MIT License. See `LICENSE` for details.
