# Bob Agent

## Overview

Bob Agent is a **local-first autonomous multi-agent AI system**. You state a goal, and Bob decomposes it into an explicit task DAG, executes each task using a ReAct (Reason+Act) LLM agent with a real tool registry, and every step lands on an inspectable event stream. Your data stays on your machine — no cloud lock-in, no telemetry by default.

Bob Agent provides **two interfaces** for interacting with the system:

- **CLI (Command-Line Interface)** — A powerful terminal REPL with slash commands for power users who prefer keyboard-driven workflows.
- **Web Dashboard** — A ChatGPT-style admin dashboard built with Next.js for visual task management, configuration, and monitoring.

After the initial CLI setup, you can choose to stick with the CLI or switch to the web interface — or use both simultaneously.

## Features

### Core Capabilities

- **Real LLM Task Execution** — Goals are run through a ReAct loop: the model reasons in text and calls typed tools via fenced code blocks. Works with any LLM provider.
- **12 LLM Providers + Offline Echo Mode** — OpenAI, Anthropic, Groq, Ollama, OpenRouter, Together, Mistral, Gemini, DeepSeek, HuggingFace, FreeLLMAPI, TokenRouter. Boots with zero keys configured.
- **Tool Registry** — Shell, file operations, web fetch, memory recall/remember, task inspection, OpenConnector SaaS actions, and MCP servers.
- **OpenConnector Integration** — Self-hosted connector gateway with 1,000+ SaaS providers and 10,000+ actions via HTTP Runtime API and implicit MCP-over-HTTP server.
- **MCP Client** — Connect to stdio servers (npx/uvx/local binaries) and streamable HTTP endpoints with session-id replay.
- **Pluggable Skills** — Pluggable instruction packs users and agents can create, import, enable, and configure.
- **SOUL.md Identity** — The agent's character, injected into every model call. Edit one file to change who Bob is.
- **Approvals + Permission Gate** — Risky actions pause for explicit approval, never slip through silently.
- **Recordings & Replay, Recipes, Cost Tracking, Telegram Gateway**

### Interfaces

- **CLI Interface** — Full-featured terminal REPL with slash commands including live configuration changes.
- **Web Dashboard** — ChatGPT-style admin interface with kanban board, chat, approvals, workspaces, vault, settings, and more.
- **Telegram Gateway** — Interact with Bob via Telegram (polling or webhook mode).

## Installation

### Prerequisites

- **Python 3.12+** — Required for the backend
- **Node.js 18+** — Required for the web dashboard
- **Git** — For cloning the repository
- **Redis** *(optional)* — Only needed for the background RQ worker (durable task queueing, scheduled jobs, Telegram/insights processing). The CLI, API, and web dashboard run without it. `make up` starts Redis + OpenConnector via Docker.

### Windows

#### Option 1: One-Line Installer (Recommended)

```powershell
irm https://raw.githubusercontent.com/johan-droid/Bob-Agent/main/agent-system/bootstrap/install.ps1 | iex
```

The installer checks prerequisites and runs the bootstrapper. A packaged
`BobAgent-Setup.exe` release is not published yet; use Option 2 (From Source)
if the one-liner is unavailable.

#### Option 2: From Source

1. Install [Python 3.12+](https://www.python.org/downloads/windows/) and [Node.js 18+](https://nodejs.org/).
2. Install [Git](https://git-scm.com/download/win).
3. Clone the repository:
   ```powershell
   git clone https://github.com/johan-droid/Bob-Agent.git
   cd BobAgent
   ```
4. Run the bootstrapper:
   ```powershell
   python bootstrap/bootstrap.py --yes
   ```

### macOS

#### Option 1: One-Line Installer (Recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/johan-droid/Bob-Agent/main/agent-system/bootstrap/install.sh | bash
```

The installer checks prerequisites and runs the bootstrapper. A Homebrew tap
is not published yet; use Option 2 (From Source) if the one-liner is
unavailable.

#### Option 2: From Source

1. Install prerequisites:
   ```bash
   brew install python@3.12 node git
   ```
2. Clone the repository:
   ```bash
   git clone https://github.com/johan-droid/Bob-Agent.git
   cd BobAgent
   ```
3. Run the bootstrapper:
   ```bash
   python3 bootstrap/bootstrap.py --yes
   ```

### Linux

#### Option 1: One-Line Installer (Recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/johan-droid/Bob-Agent/main/agent-system/bootstrap/install.sh | bash
```

The installer checks prerequisites and runs the bootstrapper. No distro
packages (`bob-agent`) exist yet; use Option 2 (From Source) if the one-liner
is unavailable.

#### Option 2: From Source

1. Install prerequisites:
   ```bash
   # Debian/Ubuntu
   sudo apt install python3.12 python3.12-venv nodejs npm git

   # Fedora
   sudo dnf install python3.12 nodejs npm git

   # Arch
   sudo pacman -S python nodejs npm git
   ```
2. Clone the repository:
   ```bash
   git clone https://github.com/johan-droid/Bob-Agent.git
   cd BobAgent
   ```
3. Run the bootstrapper:
   ```bash
   python3 bootstrap/bootstrap.py --yes
   ```

## Setup Flow

After installation, Bob Agent requires initial configuration. The setup flow is designed to get you running quickly while allowing full customization.

### Step 1: Initial CLI Setup

Run the setup wizard from your terminal:

```bash
cd agent-system
make setup
```

This interactive wizard will:
- Generate required secrets
- Configure your LLM provider (or use offline echo mode)
- Set up authentication
- Configure storage paths

Alternatively, for a fully automatic setup:

```bash
python3 bootstrap/bootstrap.py --yes
```

### Step 2: Choose Your Interface

After basic CLI setup, you can choose how to interact with Bob Agent:

#### Option A: CLI Only

If you prefer the terminal, you're ready to go:

```bash
make start    # Start the backend server
make chat     # Open the interactive REPL
```

#### Option B: Web Dashboard

To use the web interface:

```bash
cd ../agent-system/web
npm install
npm run dev
```

Then open your browser and navigate to `http://localhost:3000`.

#### Option C: Both CLI and Web

You can run both interfaces simultaneously — they share the same backend:

```bash
# Terminal 1: Backend
cd agent-system
make start

# Terminal 2: Web Dashboard
cd agent-system/web
npm run dev

# Terminal 3: CLI (optional)
cd agent-system
make chat
```

## Usage

### CLI Interface

The CLI provides a powerful REPL with slash commands:

```bash
make chat
```

**Common CLI Commands:**
- `/help` — Show available commands
- `/model set <provider> [model]` — Switch LLM provider on the fly
- `/settings set <key> <value>` — Change any configuration live
- `/tools` — List available tools and their status
- `/memory <fact>` — Store a fact in the vault
- `/recall <query>` — Search the vault for relevant facts
- `/approve <id>` — Approve a pending action

**AgentCTL Commands:**
```bash
agentctl sessions list          # List all sessions
agentctl tasks list --session <id>  # List tasks in a session
agentctl approvals list         # List pending approvals
agentctl settings list          # List all settings
agentctl skills list            # List available skills
```

### Web Dashboard

The web dashboard provides a ChatGPT-style interface at `http://localhost:3000`.

**Dashboard Pages:**
- **Chat** — Interactive chat interface with the agent
- **Kanban** — Visual task board showing task states
- **Approvals** — Review and approve/deny risky actions
- **Workspaces** — Browse and manage workspace files
- **Vault** — View and search memory notes
- **Settings** — Configure all aspects of the system
- **Cost** — Track LLM usage and costs
- **Schedule** — Manage scheduled jobs
- **Recipes** — Create and execute reusable workflows
- **Insights** — View generated insights and analytics

## Configuration

### Configuration Methods

Bob Agent can be configured in multiple ways:

1. **Web Dashboard** — Navigate to Settings page for a visual configuration interface
2. **CLI** — Use `agentctl settings` commands
3. **Environment Variables** — Set in `.env.local` file
4. **Setup Wizard** — Run `make setup` or `agentctl settings wizard`

### Key Configuration Options

| Category | Key | Description |
|----------|-----|-------------|
| **Core** | `DEFAULT_PROVIDER` | LLM provider (echo, openai, anthropic, groq, etc.) |
| **Core** | `API_PORT` | Backend HTTP port (default: 8000) |
| **Core** | `REDIS_URL` | Redis connection URL (only used by the background worker) |
| **Auth** | `AGENT_BOOTSTRAP_SECRET` | Secret for API authentication |
| **Tools** | `TOOLS_REQUIRE_APPROVAL` | Require approval for risky actions |
| **Tools** | `TOOLS_SHELL_MODE` | Shell execution mode (sandbox/local/off) |
| **Storage** | `VAULT_PATH` | Path to the Obsidian vault |
| **Integrations** | `OPENCONNECTOR_BASE_URL` | OpenConnector gateway URL |
| **Integrations** | `MCP_SERVERS` | MCP servers configuration (JSON) |
| **Telegram** | `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| **Cost** | `DAILY_BUDGET_USD` | Daily spending limit |

### Configuration Precedence

```
Environment Variables > .env.local > .env > Built-in Defaults
```

### Provider Configuration

Bob Agent supports 12+ LLM providers. Configure at least one:

```bash
# OpenAI
agentctl settings set OPENAI_API_KEY sk-...

# Anthropic
agentctl settings set ANTHROPIC_API_KEY sk-ant-...

# Groq (generous free tier)
agentctl settings set GROQ_API_KEY gsk-...

# Ollama (local, keyless)
agentctl settings set OLLAMA_BASE_URL http://localhost:11434/v1

# Offline echo mode (no API key required)
agentctl settings set DEFAULT_PROVIDER echo
```

## Troubleshooting

### Common Issues

#### CLI Issues

| Symptom | Solution |
|---------|----------|
| `make chat` → "No API token…" | Run `make setup`, then restart `make start` |
| `Port 8000 in use` | Stop the other process or set `API_PORT` to a different value |
| Worker errors on Redis | Run `make up` to start Redis via Docker |
| Provider test fails | Check `settings get <PROVIDER>_BASE_URL` and verify your API key |
| `settings check` fails | Check for typos in `.env.local`; use `settings unset` to reset |

#### Web Dashboard Issues

| Symptom | Solution |
|---------|----------|
| Dashboard won't load | Ensure backend is running (`make start`) |
| "Backend unreachable" error | Check that `AGENT_SYSTEM_API_URL` points to your backend |
| Blank page or errors | Check browser console for errors; ensure Node.js 18+ is installed |
| Changes not saving | Verify write permissions to `.env.local` |
| WebSocket errors | Check that the backend is running and accessible |

#### Platform-Specific Issues

**Windows:**
- If `make` is not recognized, use Git Bash or WSL
- For Python errors, ensure Python is added to PATH during installation
- If ports are blocked, run PowerShell as Administrator

**macOS:**
- If `python3` is not found, install Xcode Command Line Tools: `xcode-select --install`
- For permission errors, avoid using `sudo` with npm; fix permissions instead
- If Homebrew Python is used, ensure it's in your PATH

**Linux:**
- If `uv` is not found, install it: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- For Redis connection errors, ensure Redis is running: `sudo systemctl start redis`
- Check firewall settings if ports are blocked

### Getting Help

- Run `agentctl doctor` for an environment probe
- Run `settings check` to validate your configuration
- Check the logs in the `data/` directory
- Open an issue on GitHub with the output of `agentctl doctor`

## License

This project is licensed under the MIT License.

## Project Structure

```
Bob Agent/
├── agent-system/           # Main application
│   ├── backend/            # FastAPI backend + agent logic
│   │   └── src/agent_system/
│   │       ├── agents/     # ReAct agent + specialists
│   │       ├── services/   # Tools, MCP, OpenConnector, etc.
│   │       ├── api/        # FastAPI routes
│   │       ├── cli/        # agentctl commands
│   │       ├── infra/      # Database, events
│   │       └── domain/     # Core logic
│   ├── web/                # Next.js dashboard
│   ├── bootstrap/          # One-shot installer
│   ├── docs/               # Documentation
│   ├── skills/             # Built-in skills
│   └── tests/              # Test suite
├── README.md
└── .gitignore
```