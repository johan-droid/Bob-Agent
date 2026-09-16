---
title: Deployment
type: runbook
project: Agent System
status: locked
updated: 2026-09-06
up: "[[00_Index]]"
---

# 9. Deployment

> **Source:** Spec v3.0 §9

## Install

```bash
# Full stack
uv venv && uv sync   # deps from backend/pyproject.toml + uv.lock (no requirements.txt)
playwright install --with-deps
# pytesseract setup (macOS: brew install tesseract; Linux: apt-get install tesseract-ocr)
cp .env.example .env
docker compose up -d redis

# DB
alembic upgrade head

# Service
systemctl --user enable agent-system.service
systemctl --user start agent-system.service

# Dashboard + API
npm run build && npm start   # localhost:3000
# API on localhost:8000
```

## Environment Variables (`.env.example`)

| Var | Purpose |
| --- | --- |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | Primary LLM providers |
| `REDIS_URL` | Default `redis://localhost:6379/0` |
| `DATABASE_URL` | Default SQLite file; Postgres optional |
| `VAULT_PATH` | Absolute path to Obsidian vault |
| `WORKSPACES_DIR` | Where coding workspaces live |
| `TEMPLATES_DIR` | Workspace template snapshots |
| `RECORDINGS_DIR` | Behavior recording `.jsonl` files |
| `OUTPUTS_DIR` | Generated documents |
| `DAILY_BUDGET_USD` | Default cost budget |
| `API_PORT` / `WEB_PORT` | Default 8000 / 3000 |

## Service Management

| OS | Mechanism |
| --- | --- |
| Linux | `systemctl --user` unit `agent-system.service` |
| macOS | `launchd` plist (LoadAgent) |
| Windows | NSSM service wrapper |

## Health Checks

- API: `GET http://localhost:8000/api/v1/health`
- Dashboard: `http://localhost:3000` loads
- Redis: `redis-cli ping` → PONG
- DB: `alembic current` matches head
- CLI: `agentctl status` all-green panel

## Packaging

`uv + PyInstaller` for one-command install of the CLI binary (v3).

## Related

- [[03_Tech_Stack]] · [[19_Execution_Plan]]
