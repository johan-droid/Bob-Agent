.PHONY: install dev test lint typecheck migrate up down check start setup bootstrap doctor chat settings setup-web web qa-sandbox-image

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------
UNAME_S := $(shell uname -s 2>/dev/null || echo Windows)
ifeq ($(UNAME_S),Linux)
    PLATFORM := Linux
    PIPEXE := python3
else ifeq ($(UNAME_S),Darwin)
    PLATFORM := macOS
    PIPEXE := python3
else
    PLATFORM := Windows
    PIPEXE := python
endif

# ---------------------------------------------------------------------------
# One-shot installer: detects the OS, installs Python 3.12+/uv/git/Redis,
# fetches deps, migrates, and launches the setup wizard.
#   make bootstrap            # interactive
#   make bootstrap YES=--yes  # fully automatic
# Platform-specific notes:
#   Linux:   Ensure docker group membership: sudo usermod -aG docker $$USER && newgrp docker
#   macOS:   Docker Desktop required; Rosetta 2 for Apple Silicon
#   Windows: Run in PowerShell as Admin; WSL2 recommended
# ---------------------------------------------------------------------------
bootstrap:
	@echo "[make] Running bootstrap on $(PLATFORM)..."
	$(PIPEXE) bootstrap/bootstrap.py $(YES)

# Probe the machine and report what setup needs (no changes made).
doctor:
	cd backend && uv run agentctl doctor

install:
	cd backend && uv sync
	uv pip install --python backend/.venv/bin/python -e backend || true

dev:
	cd backend && uv run uvicorn agent_system.api.main:app --reload --port 8000

start: up migrate
	@echo "Starting backend (port 8000) + RQ worker…"
	@cd backend && uv run uvicorn agent_system.api.main:app --port 8000 & \
	cd backend && uv run python -m agent_system.worker

setup:
	cd backend && uv run agentctl setup

# Full settings CLI: every key and credential (list/get/set/unset/wizard).
settings:
	cd backend && uv run agentctl settings $(ARGS)

# Interactive chat REPL with slash commands.
# One-liner:  make chat   (or: cd backend && uv run agentctl chat)
chat:
	cd backend && uv run agentctl chat

test:
	cd backend && uv run pytest -q

lint:
	cd backend && uv run ruff check src tests
	cd backend && uv run ruff format --check src tests

typecheck:
	cd backend && uv run mypy src

migrate:
	cd backend && uv run alembic upgrade head

db-reset:
	@echo "[make] Resetting database..."
	rm -f backend/data/agent_system.db* backend/data/e2e_worker_check.db*
	cd backend && uv run alembic upgrade head
	@echo "[make] Database reset complete with fresh schema."

check: lint typecheck test

up:
	docker compose up -d

down:
	docker compose down

# Build the QA sandbox image that untrusted generated tests run inside
# (Phase 13). It is a local build artifact — it exists on no registry — so
# this target is what makes the Docker-gated QA path runnable. Idempotent.
qa-sandbox-image:
	docker build -t agent-system/qa-sandbox:latest -f backend/docker/qa-sandbox.Dockerfile backend/docker

# ---------------------------------------------------------------------------
# Web Dashboard Targets
# ---------------------------------------------------------------------------

# Setup web dashboard dependencies (first time only)
# Usage: make setup-web
setup-web:
	@echo "[make] Setting up web dashboard..."
	cd web && npm install
	@echo "[make] Web dashboard dependencies installed."

# Start the web dashboard development server
# Usage: make web
web:
	@echo "[make] Starting web dashboard on http://localhost:3000..."
	cd web && npm run dev

# Start Bob Agent as a Standalone Desktop Application
# Usage: make app (or make desktop)
app:
	@echo "[make] Launching Bob Agent Standalone Desktop App..."
	cd web && npm run app

desktop: app

# Build web dashboard for production
# Usage: make web-build
web-build:
	@echo "[make] Building web dashboard for production..."
	cd web && npm run build

# Start web dashboard in production mode
# Usage: make web-start
web-start:
	@echo "[make] Starting web dashboard (production)..."
	cd web && npm start

# ---------------------------------------------------------------------------
# Platform info
# ---------------------------------------------------------------------------

# Display detected platform information
# Usage: make platform
platform:
	@echo "Platform: $(PLATFORM)"
	@echo "Python: $(PIPEXE)"
	@echo "OS: $(UNAME_S)"
