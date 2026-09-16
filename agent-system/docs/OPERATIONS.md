# Operations

## Install paths

| Situation | Command |
|---|---|
| Brand-new machine | `python3 bootstrap/bootstrap.py` (`--yes` for automatic) |
| This checkout | `make install && make migrate && make setup && make start` |
| Published one-liners | `curl -fsSL <BASE>/bootstrap/install.sh \| bash` · `irm <BASE>/bootstrap/install.ps1 \| iex` (`BOB_BOOTSTRAP_BASE` / `BOB_REPO_URL` override) |

The bootstrapper detects OS/distro/arch, installs Python 3.12+/uv/git,
starts Redis via compose (unless `--no-docker`), syncs deps, migrates, and
hands off to the setup wizard (`--skip-setup` to skip it).

## Make targets

```
make install    uv sync + editable install
make migrate    alembic upgrade head
make setup      quick wizard → .env.local
make settings ARGS="list"   full settings CLI (ARGS passes through)
make bootstrap [YES=--yes]  one-shot installer
make doctor     environment probe (no changes)
make start      Redis + migrate + API :8000 + RQ worker
make chat       interactive REPL (needs make start)
make dev        API with --reload (no worker)
make up / down  docker compose Redis (+ OpenConnector) up/down
make test / lint / typecheck / check   pytest · ruff · mypy --strict

# Web Dashboard
make setup-web  Install web dashboard dependencies (npm install)
make web        Start web dashboard development server (:3000)
make web-build  Build web dashboard for production
make web-start  Start web dashboard in production mode

# Platform info
make platform   Display detected platform information
```

## Web Dashboard Operations

The web dashboard provides a ChatGPT-style interface for managing Bob Agent. It runs as a Next.js application that proxies API requests to the backend.

### Starting the Dashboard

```bash
cd web
npm install      # Install dependencies (first time only)
npm run dev      # Start development server
```

Then open `http://localhost:3000` in your browser.

### Production Build

```bash
cd web
npm run build    # Build for production
npm start        # Start production server
```

### Dashboard Pages

| Page | Route | Description |
|------|-------|-------------|
| Chat | `/chat` | Interactive chat interface with the agent |
| Kanban | `/kanban` | Visual task board showing task states |
| Approvals | `/approvals` | Review and approve/deny risky actions |
| Workspaces | `/workspace` | Browse and manage workspace files |
| Vault | `/vault` | View and search memory notes |
| Settings | `/settings` | Configure all aspects of the system |
| Cost | `/cost` | Track LLM usage and costs |
| Schedule | `/schedule` | Manage scheduled jobs |
| Recipes | `/recipes` | Create and execute reusable workflows |
| Insights | `/insights` | View generated insights and analytics |
| Templates | `/templates` | Manage task templates |
| Outputs | `/outputs` | View task outputs and artifacts |
| Reasoning | `/reasoning` | Inspect agent reasoning chains |
| Audit | `/audit` | View audit logs |

### API Proxy

The dashboard proxies `/api/v1/*` requests to the FastAPI backend (default: `http://127.0.0.1:8000`). Configure the backend URL:

```bash
# Environment variable (optional, defaults to http://127.0.0.1:8000)
export AGENT_SYSTEM_API_URL=http://localhost:8000
```

The proxy is implemented in `web/src/app/api/v1/[...path]/route.ts` and forwards:
- HTTP method
- Authorization, Content-Type, and Accept headers
- Request body (for non-GET/HEAD requests)

### Real-time Events

The dashboard uses an `EventPoller` class (`web/src/lib/api.ts`) that:
- Polls `/api/v1/events?after_sequence=N` every 2 seconds
- Implements resume-from-sequence to avoid losing or duplicating events on reconnect
- Updates the UI in real-time as events arrive
## First-run checklist

1. `make setup` (or `settings wizard`) — generates secrets at minimum.
2. **Restart `make start`** — the server reads config at boot only.
3. `make chat` — authenticates itself; type a goal.
4. `settings check` — validates the effective config anytime.
5. Optional integrations (each lights up *live*, no restart):
    - **OpenConnector** — `docker compose up openconnector`, then
      `agentctl settings set OPENCONNECTOR_BASE_URL http://localhost:3000`.
      Verify with `/tools` in the chat.
    - **MCP servers** — `agentctl settings set MCP_SERVERS '<json>'`
      (see [Configuration](CONFIGURATION.md) §MCP), verify with `/tools`.
    - **A2A delegation** — off by default (`A2A_ENABLED=false` → `503`);
      enable via `agentctl settings set A2A_ENABLED true`, then delegate with
      `POST /api/v1/a2a/delegate` (each delegation needs a fresh per-target approval).
    - **Backups** — nightly `nightly-backup` cron runs when `SCHEDULER_ENABLED=true`
      (SQLite only); run ad-hoc with `agentctl backup run`, list with `agentctl backup list`.
    - **Provider switch on the fly** — `/model set groq llama-3.3-70b` — no
      restart, no `make setup` re-run.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `make chat` → "No API token…" | Run `make setup`, restart `make start` |
| `Port 8000 … in use` | `agentctl doctor` confirms; stop the other process or set `API_PORT` |
| Worker errors on Redis | `make up` (Docker) or point `REDIS_URL` at a live server |
| Provider test fails | `settings get <PROVIDER>_BASE_URL`, check key, retry `POST /api/v1/model-routing/test` or `/model test` |
| `settings check` fails to load | Typo'd value in `.env.local` — the error names the field; `settings unset` it |
| Telegram silent | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_ALLOWED_CHAT_IDS` set? Gateway 503s otherwise by design |
| `GET /` 404s | Normal — no root route; API lives under `/api/v1` |
| Only Docker-gated test fails | Expected without a Docker sandbox (`test_real_docker_sandbox_runs_untrusted_test`) |
| `/tools` shows `○ openconnector` | `OPENCONNECTOR_BASE_URL` unset → `docker compose up openconnector` + `settings set OPENCONNECTOR_BASE_URL http://localhost:3000` |
| `openconnector_execute` → 403 `action_not_allowed` | Action blocked by the gateway policy — set `OOMOL_CONNECT_ALLOWED_ACTIONS` on the server, or pick an allowed Action id from `openconnector_list` |
| `mcp_call` → 406 Not Acceptable | Only possible if a custom `MCP_SERVERS` entry overrides the implicit server with a bad `Accept` header — remove the `Accept` header from the entry |
| MCP stdio server spawns but times out | Ensure the binary is on `PATH` and `timeout` is big enough (default 30s); try the command manually |
| Scheduler never fires | `settings get SCHEDULER_ENABLED` (default true) + `settings get SCHEDULER_TIMEZONE`; job rows live in `scheduled_jobs` |

## Web Interface Troubleshooting

| Symptom | Fix |
|---|---|
| Dashboard won't load | Ensure backend is running (`make start`), then refresh |
| "Backend unreachable" error | Check `AGENT_SYSTEM_API_URL` env var; ensure backend is running on the configured port |
| Blank page or white screen | Check browser console for errors; clear browser cache; ensure Node.js 18+ is installed |
| 404 errors on API calls | Backend may not be running; verify with `curl http://localhost:8000/api/v1/health` |
| Settings not saving | Verify write permissions to `.env.local`; check backend logs for errors |
| WebSocket/event errors | Normal during backend restart; the EventPoller will automatically resume |
| Slow dashboard performance | Check Redis connection; verify backend resources (CPU/memory) |
| "Module not found" errors | Run `npm install` in the `web/` directory |
| Build errors | Delete `.next` folder and run `npm run build` again |
| Port 3000 already in use | Kill the process using port 3000 or set `PORT=3001` env var |

### Browser Console Debugging

If the dashboard isn't working correctly:

1. Open browser developer tools (F12)
2. Check the **Console** tab for JavaScript errors
3. Check the **Network** tab for failed API requests
4. Look for CORS errors (should not occur with the proxy)
5. Verify the API proxy is working: `http://localhost:3000/api/v1/health` should return `{"status":"ok"}`

### Resetting the Dashboard

If the dashboard is in a broken state:

```bash
cd web
rm -rf .next          # Clear build cache
npm install          # Reinstall dependencies
npm run dev          # Restart development server
```

## Platform-Specific Setup

### Windows

#### Prerequisites
- [Git for Windows](https://git-scm.com/download/win) (includes Git Bash)
- [Python 3.12+](https://www.python.org/downloads/windows/) or via winget
- [Node.js 18+](https://nodejs.org/)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (for Redis)

#### Package Manager Options
- **winget** (built-in on Windows 10/11): `winget install -e --id Python.Python.3.12`
- **Chocolatey**: `choco install python git nodejs docker-desktop -y`
- **Scoop**: `scoop install python git nodejs docker`

#### Installation Steps

1. **Install prerequisites** (see above)

2. **Enable WSL2 (recommended):**
   ```powershell
   wsl --install
   ```

3. **Clone the repository:**
   ```powershell
   git clone https://github.com/your-org/bob-agent.git
   cd BobAgent
   ```

4. **Run the bootstrapper (automatic):**
   ```powershell
   python bootstrap/bootstrap.py --yes
   ```
   The bootstrapper will detect your platform and install missing dependencies.

5. **Start the backend:**
   ```powershell
   cd agent-system
   make start
   ```

6. **Start the web dashboard (new terminal):**
   ```powershell
   cd agent-system/web
   npm install
   npm run dev
   ```

#### Windows Notes
- Use **Git Bash** or **WSL** for the best experience with `make` commands
- If `python` is not recognized, use `py` or ensure Python is in PATH
- If `make` is not found, install via `choco install make` or use Git Bash
- Run PowerShell as Administrator if ports are blocked
- Docker Desktop must be running for Redis (or install Redis manually)
- For Redis without Docker: use WSL2 (`wsl --install`, then install Redis in WSL)
- On Windows 11, you can also use the Microsoft Store version of Python

#### Windows Troubleshooting
| Issue | Solution |
|-------|----------|
| `python` not found | Use `py` launcher or add Python to PATH |
| `make` not found | Install via `choco install make` or use Git Bash |
| Docker not running | Start Docker Desktop from Start Menu |
| Port 8000 blocked | Run PowerShell as Administrator |
| WSL2 not available | Enable in Windows Features: `dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart` |

### macOS

#### Prerequisites
- [Homebrew](https://brew.sh/) (recommended)
- [Xcode Command Line Tools](https://developer.apple.com/xcode/resources/)

#### Installation Steps

1. **Install prerequisites:**
   ```bash
   xcode-select --install
   brew install python@3.12 node git docker
   ```

2. **Clone the repository:**
   ```bash
   git clone https://github.com/your-org/bob-agent.git
   cd BobAgent
   ```

3. **Run the bootstrapper (automatic):**
   ```bash
   python3 bootstrap/bootstrap.py --yes
   ```

4. **Start the backend:**
   ```bash
   cd agent-system
   make start
   ```

5. **Start the web dashboard (new terminal):**
   ```bash
   cd agent-system/web
   npm install
   npm run dev
   ```

#### macOS Notes
- If `python3` is not found, ensure Homebrew Python is in PATH: `export PATH="/usr/local/opt/python@3.12/libexec/bin:$PATH"`
- For permission errors with npm, avoid `sudo`; fix permissions or use a node version manager
- Docker Desktop must be running for Redis (or install via `brew install redis`)
- On Apple Silicon Macs, ensure Rosetta 2 is installed: `softwareupdate --install-rosetta --agree-to-license`

#### macOS Troubleshooting
| Issue | Solution |
|-------|----------|
| `brew` not found | Install Homebrew: `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"` |
| `python3` points to old version | Use `python3.12` or update PATH to include Homebrew Python |
| Docker not running | Start Docker Desktop from Applications |
| Port 8000 blocked | Check firewall in System Preferences > Security & Privacy |
| Apple Silicon issues | Ensure Rosetta 2 is installed for x86 containers |

### Linux

#### Prerequisites
- Package manager (apt, dnf, or pacman)
- Build tools

#### Installation Steps

**Debian/Ubuntu:**
```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3-pip nodejs npm git curl docker.io docker-compose
```

**Fedora:**
```bash
sudo dnf install -y python3.12 nodejs npm git curl docker docker-compose
```

**Arch Linux:**
```bash
sudo pacman -S python nodejs npm git curl docker docker-compose
```

**Alpine Linux:**
```bash
sudo apk add python3 py3-pip nodejs npm git curl docker docker-compose
```

**openSUSE:**
```bash
sudo zypper install -y python312 nodejs npm git curl docker docker-compose
```

**Common Steps:**
1. **Clone the repository:**
   ```bash
   git clone https://github.com/your-org/bob-agent.git
   cd BobAgent
   ```

2. **Run the bootstrapper (automatic):**
   ```bash
   python3 bootstrap/bootstrap.py --yes
   ```

3. **Start the backend:**
   ```bash
   cd agent-system
   make start
   ```

4. **Start the web dashboard (new terminal):**
   ```bash
   cd agent-system/web
   npm install
   npm run dev
   ```

#### Linux Notes
- If `uv` is not found, install it: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- For Redis, either use Docker (`make up`) or install directly: `sudo apt install redis-server`
- Ensure your user is in the `docker` group to run Docker without sudo: `sudo usermod -aG docker $USER`
- Check firewall settings if ports are blocked: `sudo ufw allow 8000/tcp && sudo ufw allow 3000/tcp`

#### Linux Troubleshooting
| Issue | Solution |
|-------|----------|
| `python3.12` not found | Use deadsnakes PPA (Ubuntu): `sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.12` |
| `docker` permission denied | Add user to docker group: `sudo usermod -aG docker $USER && newgrp docker` |
| Port 8000 blocked | Open firewall: `sudo ufw allow 8000/tcp` |
| `npm` permission errors | Fix npm permissions or use nvm |
| Docker not installed | Use convenience script: `curl -fsSL https://get.docker.com | sh` |

## Hygiene

- Never commit `.env.local` (gitignored), `data/`, or `~/.config/bob-agent/`.
- Secrets are masked in `settings list`, setup previews, and logs; profiles
  never store them.
- Backend runs on `:8000`, Redis on `:6379`, OpenConnector on `:3000` by
  default; all configurable.
- `docker compose up` now also brings up the optional `openconnector` gateway
  (image `ghcr.io/oomol-lab/open-connector:latest`, data volume
  `openconnector_data`). Generate `OOMOL_CONNECT_ENCRYPTION_KEY` and
  `OOMOL_CONNECT_ADMIN_TOKEN` before first boot:
  `openssl rand -base64 32` for each.

## Observability (optional profile)

> **CONFIGURATION REQUIRED — artifacts not yet in repo.** The compose
> example below is documentation only: `docker-compose.observability.yml`
> and `observability/otel-collector.yml` (+ `observability/prometheus.yml`)
> do not exist in the repo yet.
> TODO: add `docker-compose.observability.yml` +
> `observability/otel-collector.yml` before this profile can be started.
> Until then, telemetry stays off (default) with only in-memory counters.

Telemetry is **off by default**: with `OTEL_EXPORTER_OTLP_ENDPOINT` unset the
server exports nothing, imports no OTel packages, and keeps only cheap
in-memory counters (visible to tests via `Metrics.snapshot()`). To observe a
deployment:

1. `pip install agent-system[telemetry]` (or add the `telemetry` extra).
2. Bring up the optional observability stack (Prometheus + Grafana, pulling
   from an OpenTelemetry collector):

```yaml
# docker-compose.observability.yml — optional profile, not started by default.
services:
  otel-collector:
    image: otel/opentelemetry-collector-contrib:0.111.0
    command: ["--config=/etc/otel-collector.yml"]
    volumes:
      - ./observability/otel-collector.yml:/etc/otel-collector.yml:ro
    ports:
      - "4318:4318"   # OTLP/HTTP (traces + metrics)
      - "8889:8889"   # Prometheus exporter
  prometheus:
    image: prom/prometheus:v2.53.0
    volumes:
      - ./observability/prometheus.yml:/etc/prometheus/prometheus.yml:ro
    ports:
      - "9090:9090"
  grafana:
    image: grafana/grafana:11.1.0
    ports:
      - "3000:3000"
    environment:
      GF_SECURITY_ADMIN_PASSWORD: admin
```

3. `docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d`
4. `agentctl settings set OTEL_EXPORTER_OTLP_ENDPOINT http://localhost:4318`
   and restart the API server. The server auto-instruments FastAPI +
   SQLAlchemy + httpx and exports the business metrics (`bob.task.duration`,
   `bob.model.latency`, `bob.tool.calls`, `bob.approval.latency`,
   `bob.cost.usd`) plus traces to the collector.

Smoke test: with the endpoint set and the `telemetry` extra installed,
`backend/tests/unit/test_telemetry.py::TestOtelSmoke` asserts the handle
reports `enabled`; without the extra the server stays disabled with a
warning instead of crashing (covered by
`test_missing_sdk_warns_instead_of_raising`).
