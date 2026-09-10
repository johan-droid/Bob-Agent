---
title: Tech Stack
type: spec
project: Agent System
status: locked
updated: 2026-09-06
up: "[[00_Index]]"
---

# 3. Tech Stack (Locked)

> **Source:** Spec v3.0 §3 · Decisions in this table are locked; changes require an ADR in [[22_Decision_Log]].

| Layer | Tech | Why |
| --- | --- | --- |
| Backend | Python 3.12 + FastAPI | async, rich ecosystem |
| Subagents | Process isolation + asyncio | crash safety |
| CLI | Typer + Rich (v1); Rust binary (v3) | beautiful terminal output, clean UX |
| Frontend | Next.js 15 + Tailwind + shadcn/ui | familiar stack, component library ready |
| Realtime | WebSocket + SSE (FastAPI native) | token streaming, live updates |
| Task queue | Redis + RQ | proven, simple |
| Scheduler | APScheduler + Redis jobstore | cron/interval/date/webhook |
| Storage (relational) | SQLite (local) → Postgres (optional) | zero-config first |
| Storage (vector) | LanceDB (embedded) | no external server |
| Memory (human) | Obsidian vault: `.md` + YAML frontmatter | dual-use: agent + human readable |
| Browser | Playwright (Python) | session persistence, video recording |
| Document gen | python-pptx, python-docx, weasyprint, openpyxl | battle-tested, template-based |
| Computer-use | pyautogui, pynput, pytesseract, accessibility APIs | pixel + tree targeting fallback |
| Sandboxing | Docker (code, browser) + restricted OS account (Autopilot) | strong isolation boundaries |
| Packaging | uv + PyInstaller | one-command install |
| Service mgmt | systemd (Linux), launchd (macOS), NSSM (Windows) | survive reboot |

## Conventions

- Python: `ruff` + `mypy`, async-first modules under `core/`.
- Frontend: TypeScript strict, components under `web/components/`, one folder per dashboard tab.
- Config: single `.env` + `config/*.json` (e.g. `model_selection_rules.json`).
- Ports: API `:8000`, Dashboard `:3000`, Redis `:6379`.

## Related

- Deployment commands: [[20_Deployment]]
- UI design tokens: [[16_Dashboard_UIUX]]
