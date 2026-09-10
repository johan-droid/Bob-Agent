---
title: CLI Specification
type: spec
project: Agent System
status: locked
updated: 2026-09-06
up: "[[00_Index]]"
---

# 6. CLI Tool — TUI/Output Specification

> **Source:** Spec v3.0 §6 · Entry point: `agentctl`
>
> [!warning] v3.1 CLI Rule (§34)
> CLI commands must use the **same API/domain contracts** as the web dashboard (`/api/v1`, [[18_API_Reference]]) — no separate CLI-only business logic. Support `--json` (machine-readable, stable schema), `--verbose`, `--no-color`. Stable exit codes for scripting. The Reasoning command surfaces the **Decision & Execution Trace** ([[05_Feature_Reasoning_Trace_Viewer]]) — auditable events, not private reasoning.

## 6.1 Framework & Styling

**Framework:** Typer (v1, Python) → custom Rust binary (v3) wrapping the same HTTP API.

**Styling libraries:**

- Rich (Python): tables, panels, progress bars, syntax highlighting, colors, spinners.
- Prompt-toolkit (Python): multi-line input, autocomplete, key bindings.
- Clap (Rust v3): argument parsing, subcommands.

**Color scheme (matches web dashboard [[16_Dashboard_UIUX]]):**

```
Primary: Bright Blue (#0088FF)
Success: Bright Green (#00DD00)
Warning: Bright Orange (#DD8800)
Error:   Bright Red (#DD0000)
Muted:   Dim (#888888)
```

## 6.2 Main Commands

```bash
agentctl --help
```

```
╭──────────────────────────────────────────────────────────╮
│                   🤖 AGENT SYSTEM                        │
│                Local AI Agent Orchestrator                │
│                    v3.0 — Ready                          │
╰──────────────────────────────────────────────────────────╯

Usage: agentctl [OPTIONS] COMMAND [ARGS]

Commands:
  chat         💬 Submit a goal to the supervisor agent
  status       📊 Display system status and active agents
  agents       👥 Manage agents (list, kill, inspect)
  tasks        ✅ Manage tasks (list, inspect, retry, cancel)
  workspace    🖥️  Manage coding workspaces
  vault        📚 Manage memory vault notes
  documents    📄 Manage generated documents
  browser      🌐 Manage browser sessions
  autopilot    🎮 Manage autopilot / desktop control sessions
  schedule     📅 Manage scheduled jobs
  approvals    ✔️  Manage pending approvals
  recipes      📖 Manage workflow recipes
  cost         💰 Show cost tracking and budget
  insights     💡 Generate and view insights
  memory       🧠 Query agent memory
  logs         📜 Tail service logs
  start        🚀 Start the agent service
  stop         ⏹️  Stop the agent service
  restart      🔄 Restart the agent service
  config       ⚙️  Show/edit configuration
  version      ℹ️  Show version info

Options:
  -h, --help         Show this help message
  -v, --verbose      Verbose output
  --json             Output in JSON format (for scripting)
  --no-color         Disable colored output

Examples:
  agentctl chat "Build a FastAPI scaffold"
  agentctl status
  agentctl workspace list
  agentctl vault search "auth decision"
  agentctl schedule list
  agentctl approvals list
  agentctl cost summary
```

## 6.3 Key Commands with Output

### A. Chat (`agentctl chat`)

```bash
$ agentctl chat "Research the latest AI trends and summarize"
```

```
╭──────────────────────────────────────────────────────────────╮
│ 🚀 GOAL SUBMITTED TO SUPERVISOR                             │
│ Session: session-12345                                      │
╰──────────────────────────────────────────────────────────────╯

Task Graph:
┌─────────────────────────────────────────────────────────────┐
│ Goal: Research the latest AI trends and summarize           │
│                                                             │
│  [t1] Research: "Latest AI trends 2026"                    │
│       ├─ Agent: ResearchAgent                              │
│       ├─ Status: [████████░░░░░░░░░░░░] 40%               │
│       └─ LLM: claude-sonnet-5 (est. $0.08)                │
│                                                             │
│  [t2] Summarize findings (depends on t1)                   │
│       ├─ Agent: DocumentAgent                              │
│       └─ LLM: claude-haiku-4-5 (est. $0.01)               │
│                                                             │
│  [t3] Generate PDF (depends on t2)                         │
│       └─ LLM: none (local)                                 │
└─────────────────────────────────────────────────────────────┘

Estimated Cost: $0.09 | Estimated Time: ~3 min

Real-time Output:
┌─────────────────────────────────────────────────────────────┐
│ [t1] ResearchAgent:                                          │
│ 🔍 Searching: "Latest AI trends 2026"                      │
│   → Found 8 relevant sources (1.2s)                         │
│   → Fetching content (3.4s)                                │
│   ✅ Complete: 5 key trends identified                      │
└─────────────────────────────────────────────────────────────┘

[Enter to watch live, 'q' to quit, 'a' to approve next action]
```

**Interactive mode:** press 'a' to show approval queue if any, 'q' to detach (runs in background).

### B. Status (`agentctl status`)

```
╭──────────────────────────────────────────────────────────────╮
│ 📊 SYSTEM STATUS                                             │
│ Service: ✅ Running (uptime: 23h 45m)                       │
│ API: localhost:8000 ✅    Dashboard: localhost:3000 ✅       │
│ Database: ✅ Connected (sqlite)   Redis: ✅ Connected       │
╰──────────────────────────────────────────────────────────────╯

Active Agents:                Task Queue:
│ agent-001 │ Research │ ✅ Running │    Running: 2
│ agent-002 │ Code     │ ✅ Running │    Queued: 5
│ agent-003 │ Document │ ⏳ Waiting  │    Approved (pending): 1
│ agent-004 │ Autopilot│ ⏸️  Paused  │

Cost Summary (today):
│ Spent: $2.34 / $10.00 budget (23%)                         │
│ Projected end-of-day: $3.12   Status: ✅ On track          │

Insights:
├─ 12 tasks completed today
├─ 98% success rate
└─ Most used agent: CodeAgent (35%)
```

### C. Workspace (`agentctl workspace list` / `inspect <id>`)

```
Active Workspaces:
│ ID       │ Name              │ Status   │ Size    │ Modified │
│ ws-0001  │ FastAPI Scaffold  │ ✅ Ready │ 142 KB  │ 5m ago   │
│ ws-0002  │ React Dashboard   │ ✅ Ready │ 2.1 MB  │ 2h ago   │
```

`inspect` shows: container id, file tree, recent commits (signed CodeAgent), QA status (tests/coverage), templates.

### D. Vault Search (`agentctl vault search <query>`)

```
1️⃣  Decision: Better Auth + FastAPI JWT Reconciliation
   Path: vault/Decisions/auth-reconciliation.md
   Tags: [auth, security, backend]   Relevance: 98% ⭐
```

### E. Cost Summary (`agentctl cost summary --period month`)

```
│ Spent: $18.42 / $100.00 budget (18%)                       │
│ Projected (30 days): $92.10 ✅ On track                    │

Breakdown by Agent Type:            Breakdown by LLM Model:
│ ResearchAgent  │ $8.42 (46%)      │ claude-opus-5   │ $8.92 (48%)
│ CodeAgent      │ $5.29 (29%)      │ claude-sonnet-5 │ $6.34 (34%)
│ DocumentAgent  │ $3.15 (17%)      │ claude-haiku-4-5│ $2.81 (15%)

Recommendations:
├─ 💡 Switch more summarization tasks to Claude Haiku → save $1.2/day
├─ 💡 Batch research tasks to reduce overhead → save ~8%
```

### F. Recipes (`agentctl recipes list` / `run <id>`)

```
│ [1] Blog Publishing Pipeline                               │
│     Research topic → Write article → Generate PDF + email  │
│     Parameters: topic (string), email (string)             │
│     Usage count: 12   Duration: ~15 min   Est. cost: $0.45 │
```

Run: `agentctl recipes run 1 --topic "AI Trends 2026" --email "me@example.com"`
Shows live DAG progress, pending approvals inline ('a' to approve, 'q' to quit).

### G. Approvals (`agentctl approvals list`)

- 🔴 HIGH PRIORITY: browser transactional actions (payment submit) with form preview, expiry countdown.
- 🟡 MEDIUM: file deletes with diff preview, browser auth with session note.
- Per action: [Approve] [Deny] [Always allow this type].

### H. Logs (`agentctl logs --follow --agent ResearchAgent`)

Streaming log lines with timestamps, agent id, level; ends with cost/duration/tokens per task.

## 6.4 Interactive Prompts & Input

Multi-line chat input (Ctrl+D to submit). Shell autocompletion for subcommands:

```bash
$ agentctl workspace [TAB]
  list | inspect | new | clone | delete | run | save-template
```

## 6.5 Output Formats

```bash
$ agentctl status --json        # machine-readable JSON (for scripting)
$ agentctl cost summary --csv > costs.csv
```

## Related

- Dashboard UI: [[16_Dashboard_UIUX]]
- API: [[18_API_Reference]]
