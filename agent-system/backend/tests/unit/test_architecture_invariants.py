"""Architectural Invariants — verify Telegram-only cloud agent boundaries."""

from __future__ import annotations

import pathlib

from typer.testing import CliRunner

from agent_system.cli.main import app

runner = CliRunner()


def test_obsolete_web_directory_does_not_exist() -> None:
    """The Next.js web dashboard and Electron app directory must not exist."""
    repo_root = pathlib.Path(__file__).resolve().parents[3]
    web_dir = repo_root / "agent-system" / "web"
    assert not web_dir.exists(), "web/ directory must not be present"


def test_obsolete_launch_app_script_does_not_exist() -> None:
    """The desktop launch script must not exist."""
    repo_root = pathlib.Path(__file__).resolve().parents[3]
    script = repo_root / "agent-system" / "launch-app.sh"
    assert not script.exists(), "launch-app.sh must not be present"


def test_cli_chat_command_does_not_exist() -> None:
    """The user-facing CLI chat REPL command must not be present in agentctl."""
    result = runner.invoke(app, ["chat"])
    assert result.exit_code != 0


def test_cli_web_command_does_not_exist() -> None:
    """The user-facing CLI web launcher command must not be present in agentctl."""
    result = runner.invoke(app, ["web"])
    assert result.exit_code != 0


def test_obsolete_cli_chat_module_does_not_exist() -> None:
    """The agent_system.cli.chat module must not exist."""
    cli_dir = pathlib.Path(__file__).resolve().parents[2] / "src" / "agent_system" / "cli"
    chat_file = cli_dir / "chat.py"
    assert not chat_file.exists(), "cli/chat.py must not be present"
