#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==========================================="
echo "  🤖 Starting Bob Agent Standalone App"
echo "==========================================="

# Load environment configuration safely
load_env_file() {
  local env_file="$1"
  if [ -f "$env_file" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      # Trim whitespace
      line="$(echo "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
      # Skip comments and empty lines
      [[ "$line" =~ ^#.*$ ]] && continue
      [[ -z "$line" ]] && continue
      if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
        key="${BASH_REMATCH[1]}"
        val="${BASH_REMATCH[2]}"
        # Strip outer quotes if present
        val="${val#\"}"
        val="${val%\"}"
        val="${val#\'}"
        val="${val%\'}"
        export "$key=$val" 2>/dev/null || true
      fi
    done < "$env_file"
  fi
}

load_env_file "$DIR/backend/.env.local"
load_env_file "$DIR/.env.local"

# Ensure backend and web dependencies
if [ ! -d "$DIR/backend/.venv" ]; then
  echo "[1/3] Syncing backend environment..."
  cd "$DIR/backend" && uv sync
fi

if [ ! -d "$DIR/web/node_modules" ]; then
  echo "[2/3] Installing web dependencies..."
  cd "$DIR/web" && npm install
fi

echo "[3/3] Launching Standalone Desktop Application..."
cd "$DIR/web" && npx electron .
