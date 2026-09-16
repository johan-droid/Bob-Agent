# Cloud Deploy — Heroku (Basic 512MB)

Single-dyno cloud runtime for Bob Agent: Telegram webhook in, in-process
execution, Postgres persistence. No Docker, no Redis, no local files.

## Architecture

```
Telegram ──webhook──► web dyno (uvicorn, $PORT)
                      ├─ FastAPI /api/v1 (+ /telegram/webhook)
                      ├─ Orchestrator in-process (services/cloud.drive_session)
                      │    └─ ReAct loop → SubprocessJail (no Docker)
                      ├─ Postgres (addon) — sessions/tasks/events/approvals/
                      │    model-calls + memory_notes (vault)
                      └─ ephemeral scratch — workspaces/outputs/recordings
                         (lost on restart; never durable)
```

## Why it looks like this

| Local | Cloud | Reason |
|---|---|---|
| DockerSandbox | SubprocessJail (`HEROKU_JAIL=true`) | No Docker daemon on dynos |
| RQ worker + Redis | `drive_session` in-process (`CLOUD_INLINE_RUN=true`) | One Basic dyno; no Redis addon |
| SQLite + nightly file backups | Postgres + Heroku PG backups | Ephemeral FS; `BackupService` is SQLite-only (job auto-skipped on Postgres) |
| Obsidian vault dir | `memory_notes` table (`CLOUD_VAULT_DB=true`) | Ephemeral FS; same scrubbed content |
| Telegram polling | Webhook (`TELEGRAM_WEBHOOK_SECRET` set) | Polling burns dyno hours + duplicates |
| `api_port` | `$PORT` (`effective_port`) | Platform assigns a random port |

## Prerequisites

- Heroku CLI logged in (`heroku auth:whoami`), app on `heroku-24` stack.
- Addons budget ≈ $12–16/mo: Basic web dyno ($7) + Postgres Essential-0 (~$5–9).
- One LLM provider key (e.g. `GROQ_API_KEY`), a Telegram bot token
  (@BotFather), and your Telegram chat id.

## Deploy (step by step)

App root for Heroku is `backend/` (holds `pyproject.toml` + `uv.lock` +
`.python-version` + `Procfile`, which the Python/uv buildpack requires).
Deploy it as a subtree:

```bash
# 1. Create the app + database
heroku create <app-name> --stack heroku-24
heroku addons:create heroku-postgresql:essential-0 --app <app-name>

# 2. Push only backend/ as the app root
# NOTE: agent-system is currently a submodule entry of the outer repo,
# so `git subtree push --prefix agent-system/backend` from the outer repo
# will NOT include working-tree files. Pick one:
#   (a) Absorb the submodule first (one time):
#         git rm --cached agent-system && git add agent-system \
#           && git commit -m "absorb agent-system submodule"
#       then: git subtree push --prefix agent-system/backend heroku main
#   (b) Or deploy from a standalone clone of the agent-system tree:
#         cd /tmp && git clone <agent-system-repo-url> bob-cloud \
#           && cd bob-cloud/backend && heroku git:remote --app <app-name> \
#           && git push heroku main
git subtree push --prefix agent-system/backend heroku main

# 3. The release phase runs `alembic upgrade head` automatically.
#    Verify:
heroku run 'python -c "import agent_system.api.main; print(\"boot ok\")"' --app <app-name>
curl https://<app-name>.herokuapp.com/health

# 4. Set secrets (never in git)
heroku config:set --app <app-name> \
  AGENT_BOOTSTRAP_SECRET="$(openssl rand -base64 32)" \
  API_SESSION_SECRET="$(openssl rand -base64 32)" \
  TELEGRAM_BOT_TOKEN="<bot-token>" \
  TELEGRAM_WEBHOOK_SECRET="$(openssl rand -base64 32)" \
  TELEGRAM_ALLOWED_CHAT_IDS="<your-chat-id>" \
  GROQ_API_KEY="<key>" \
  DEFAULT_PROVIDER=groq

# 5. Register the Telegram webhook (once)
curl -X POST https://api.telegram.org/bot<bot-token>/setWebhook \
  -d url=https://<app-name>.herokuapp.com/api/v1/telegram/webhook \
  -d secret_token="<TELEGRAM_WEBHOOK_SECRET>"

# 6. Talk to Bob: send a goal as a Telegram message.
#    Approve risky steps with /approve <id>, re-run failures with /retry <task_id>.
```

`app.json` encodes the same config for one-click Review Apps
(`heroku-postgresql:essential-0` + locked 512MB config vars).

## Configuration lock (Heroku config vars)

```
TOOLS_SHELL_MODE=local        # jail path (Docker unreachable)
HEROKU_JAIL=true              # route shell + execute-plugins to SubprocessJail
TOOLS_REQUIRE_APPROVAL=true   # never off in cloud
CLOUD_INLINE_RUN=true         # drive Telegram goals in-process (no RQ)
CLOUD_VAULT_DB=true           # memory_notes table (no Obsidian dir)
MCP_SERVERS=[]                # no npx/uvx subprocesses (OOM risk)
SCHEDULER_ENABLED=false       # no APScheduler thread; PG backups cover DR
MEMORY_EMBEDDING_PROVIDER=hash  # never install sentence-transformers (torch OOM)
A2A_ENABLED=false
TOOLS_MAX_ITERS=6  MAX_TASK_TOKENS=50000  MAX_OUTPUT_SIZE_MB=5
MAX_FILE_SIZE_MB=2  MAX_WORKSPACE_SIZE_MB=64  MAX_EXECUTION_TIME_SECONDS=120
```

## Security posture (read this)

- `SubprocessJail` is **containment, not isolation**: cwd confined to the
  workspace, secret env (`*_KEY`/`*_TOKEN`/`*_SECRET`/`*_URL`/…​) stripped,
  256MB address-space + 60s CPU rlimits, 1MB output cap, timeout enforced.
  A dyno compromise can still read process memory. Mitigations, all on by
  default: every execute call needs a live Telegram `/approve`; optional
  `HEROKU_SHELL_ALLOWLIST` restricts binaries (e.g. `ls,cat,echo,python3`);
  untrusted code belongs on an E2B/Modal-style remote sandbox (tracked
  follow-up, not in this pass).
- Secrets live only in Heroku config vars — never files, never logs
  (masked in `settings list`, redacted at `EventBus.emit`, scrubbed before
  every vault write incl. title/tags).
- `TELEGRAM_ALLOWED_CHAT_IDS` is the auth boundary; webhook secret is
  constant-time compared; rotate `AGENT_BOOTSTRAP_SECRET` (never the dev
  default) before any non-localhost use.

## Memory budget (Basic 512MB)

Base slug stays ~120–200MB RSS iff: no `memory`/`telemetry` extras, no
Playwright/Chromium (research uses the httpx path), `MCP_SERVERS=[]`, no
Telegram polling thread, single uvicorn worker (no gunicorn — multi-worker
would exceed quota). R15 (memory-quota) playbook: cut `TOOLS_MAX_ITERS` /
`MAX_TASK_TOKENS` first; if sustained, step up to Standard-2x (1GB) — no
code changes needed.

## What is NOT in cloud scope

- Web dashboard (`web/`, Next.js) — deploy separately (Vercel) pointed at
  the Heroku API URL, or omit.
- `agentctl backup run` — SQLite-only by design; use Heroku PG backups.
- Scheduler/APScheduler, RQ worker, OpenConnector gateway, MCP stdio
  servers, QA Docker runs, browser automation — all disabled or degraded
  by config; every disable is logged/honest (503 or clear `ToolError`).

## Verification checklist

- [ ] `GET /health` → ok, `GET /ready` → database true (Postgres).
- [ ] `POST /sessions` + Telegram goal → task SUCCEEDED, events present.
- [ ] Execute tool without approval → `NeedsApprovalError` + approval id.
- [ ] `/approve <id>` + `/retry <task_id>` → runs with approval present.
- [ ] `/memory` + `/recall` round-trip (rows in `memory_notes`).
- [ ] 3-goal burst without R15 on the metrics tab.
- [ ] `make check` green locally (lint + `mypy --strict` + full suite).
