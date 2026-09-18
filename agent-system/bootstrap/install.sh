#!/usr/bin/env bash
# Bob Agent one-liner installer (Linux / macOS).
#
#   curl -fsSL https://raw.githubusercontent.com/johan-droid/Bob-Agent/main/agent-system/bootstrap/install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/johan-droid/Bob-Agent/main/agent-system/bootstrap/install.sh | bash -s -- --yes --skip-setup
#
# Override the download base (e.g. a pinned release) with BOB_BOOTSTRAP_BASE.
# From a local checkout this script just runs ./bootstrap/bootstrap.py.
set -euo pipefail

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------
detect_platform() {
  local uname_out="$(uname -s)"
  case "$uname_out" in
    Linux*)   echo "linux" ;;
    Darwin*)  echo "macos" ;;
    CYGWIN*|MINGW*|MSYS*) echo "windows" ;;
    *)        echo "unknown" ;;
  esac
}

PLATFORM="$(detect_platform)"
BASE="${BOB_BOOTSTRAP_BASE:-https://raw.githubusercontent.com/johan-droid/Bob-Agent/main/agent-system/bootstrap}"

# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------
error_exit() {
  echo "[install] ERROR: $1" >&2
  exit 1
}

cleanup() {
  if [ -n "${TMPD:-}" ] && [ -d "$TMPD" ]; then
    rm -rf "$TMPD"
  fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Local checkout detection
# ---------------------------------------------------------------------------
here_bootstrap=""
if [ -f "bootstrap/bootstrap.py" ]; then
  here_bootstrap="bootstrap/bootstrap.py"          # run from agent-system/
elif [ -f "agent-system/bootstrap/bootstrap.py" ]; then
  here_bootstrap="agent-system/bootstrap/bootstrap.py"  # run from repo root
fi

if [ -n "$here_bootstrap" ]; then
  echo "[install] using local $here_bootstrap"
  exec python3 "$here_bootstrap" "$@"
fi

# ---------------------------------------------------------------------------
# Platform-specific notes
# ---------------------------------------------------------------------------
print_platform_notes() {
  case "$PLATFORM" in
    linux)
      echo "[install] Platform: Linux"
      echo "[install] Tips:"
      echo "  - Ensure your user is in the 'docker' group: sudo usermod -aG docker \$USER && newgrp docker"
      echo "  - If ports are blocked: sudo ufw allow 8000/tcp && sudo ufw allow 3000/tcp"
      ;;
    macos)
      echo "[install] Platform: macOS"
      echo "[install] Tips:"
      echo "  - On Apple Silicon, ensure Rosetta 2: softwareupdate --install-rosetta --agree-to-license"
      echo "  - Docker Desktop for Mac is required for container support"
      ;;
    *)
      echo "[install] Platform: $PLATFORM"
      ;;
  esac
}

# ---------------------------------------------------------------------------
# Python detection
# ---------------------------------------------------------------------------
find_python() {
  local cand
  for cand in python3.12 python3.13 python3.11 python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
      echo "$cand"
      return 0
    fi
  done
  return 1
}

print_install_hints() {
  echo "[install] No Python 3.12+ found. Install Python 3.12+ first:" >&2
  case "$PLATFORM" in
    linux)
      echo "  Ubuntu/Debian:  sudo apt-get install -y python3.12 python3.12-venv git curl" >&2
      echo "  Fedora:         sudo dnf install -y python3.12 git curl" >&2
      echo "  Arch Linux:     sudo pacman -Sy --noconfirm python git curl" >&2
      echo "  Alpine:         sudo apk add python3 git curl py3-pip" >&2
      echo "  openSUSE:       sudo zypper install -y python312 git curl" >&2
      ;;
    macos)
      if command -v brew >/dev/null 2>&1; then
        echo "  macOS (Homebrew): brew install python@3.12 git" >&2
      else
        echo "  Install Homebrew: /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"" >&2
        echo "  Then: brew install python@3.12 git" >&2
      fi
      echo "  Or download from: https://www.python.org/downloads/macos/" >&2
      ;;
    *)
      echo "  Download from: https://www.python.org/downloads/" >&2
      ;;
  esac
}

# ---------------------------------------------------------------------------
# Dependency installation hints
# ---------------------------------------------------------------------------
print_dep_hints() {
  echo "[install] Missing dependencies detected. Install them with:" >&2
  case "$PLATFORM" in
    linux)
      if command -v apt-get >/dev/null 2>&1; then
        echo "  sudo apt-get install -y git curl" >&2
      elif command -v dnf >/dev/null 2>&1; then
        echo "  sudo dnf install -y git curl" >&2
      elif command -v pacman >/dev/null 2>&1; then
        echo "  sudo pacman -Sy --noconfirm git curl" >&2
      elif command -v apk >/dev/null 2>&1; then
        echo "  sudo apk add git curl" >&2
      fi
      ;;
    macos)
      if command -v brew >/dev/null 2>&1; then
        echo "  brew install git curl" >&2
      else
        echo "  Install Homebrew first: https://brew.sh" >&2
      fi
      ;;
  esac
}

# ---------------------------------------------------------------------------
# Docker installation hints
# ---------------------------------------------------------------------------
print_docker_hints() {
  echo "[install] Docker not found. Install Docker:" >&2
  case "$PLATFORM" in
    linux)
      echo "  Ubuntu/Debian: sudo apt-get install -y docker.io docker-compose" >&2
      echo "  Fedora:        sudo dnf install -y docker docker-compose" >&2
      echo "  Arch Linux:    sudo pacman -Sy --noconfirm docker docker-compose" >&2
      echo "  Or use Docker's convenience script: curl -fsSL https://get.docker.com | sh" >&2
      ;;
    macos)
      echo "  macOS: Download Docker Desktop from https://www.docker.com/products/docker-desktop/" >&2
      echo "  Or with Homebrew: brew install --cask docker" >&2
      ;;
  esac
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
print_platform_notes

PYBIN="$(find_python)" || {
  print_install_hints
  exit 1
}

# Check for curl/wget
if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
  echo "[install] Neither curl nor wget found." >&2
  print_dep_hints
  exit 1
fi

# Check for git (warn but don't fail)
if ! command -v git >/dev/null 2>&1; then
  echo "[install] git not found — needed for cloning." >&2
  print_dep_hints
fi

# Check for docker (warn but don't fail)
if ! command -v docker >/dev/null 2>&1; then
  echo "[install] Docker not found — needed for Redis." >&2
  print_docker_hints
fi

TMPD="$(mktemp -d)"
echo "[install] Downloading bootstrap.py…"
if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$BASE/bootstrap.py" -o "$TMPD/bootstrap.py" || error_exit "Failed to download bootstrap.py"
elif command -v wget >/dev/null 2>&1; then
  wget -qO "$TMPD/bootstrap.py" "$BASE/bootstrap.py" || error_exit "Failed to download bootstrap.py"
fi

echo "[install] Running bootstrap with Python: $PYBIN"
exec "$PYBIN" "$TMPD/bootstrap.py" "$@"
