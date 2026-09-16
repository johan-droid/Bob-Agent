"""agentctl entry point — thin launcher for the CLI implementation.

Implementation lives in `agent_system.cli.main` (installed package); this
launcher matches the target repo layout (`cli/` at the repo root, Phase 0 §4).
"""

from agent_system.cli.main import app

if __name__ == "__main__":
    app()
