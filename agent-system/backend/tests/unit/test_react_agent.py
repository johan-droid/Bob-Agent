"""Unit tests: LLM react handler — registry wiring + real loop execution.

The handler executes a task goal through the ModelRouter + tool registry
(``services/agent_loop.run_tool_loop``). A stub router stands in for the
provider so the full path (tool fence -> tool execution -> memory outcome)
is exercised deterministically, offline.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_system.agents import react_agent, registry
from agent_system.config import Settings
from agent_system.infra.db import make_engine, make_session_factory, session_scope
from agent_system.infra.models import Base, EventRow


@pytest.fixture()
def factory(tmp_path: Path) -> Any:
    engine = make_engine(f"sqlite:///{tmp_path / 'react.db'}")
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture()
def clean_registry() -> Any:
    """Snapshot/restore the global agent registry around each test."""
    saved = registry.snapshot()
    yield
    registry.restore(saved)


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    skills = tmp_path / "skills"
    skills.mkdir()
    (tmp_path / "SOUL.md").write_text("Test soul.\n")
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=f"sqlite:///{tmp_path / 'react.db'}",
        vault_path=tmp_path / "vault",
        skills_dir=skills,
        soul_path=str(tmp_path / "SOUL.md"),
        workspaces_dir=tmp_path / "workspaces",
        tools_fs_roots=str(tmp_path),
        tools_shell_mode="off",
        tools_require_approval=False,
        tools_max_iters=4,
        memory_auto_remember=True,
        memory_recall_top_k=0,
        openconnector_base_url="",
        mcp_servers="[]",
        default_provider="echo",
    )


def _context(factory: Any, settings: Any) -> dict[str, Any]:
    return {
        "session_id": "ses_react0000000000000000",
        "task_id": "task_react0000000000000001",
        "agent_run_id": "run_react0000000000000001",
        "agent_type": "llm",
        "factory": factory,
        "settings": settings,
    }


def test_install_registers_llm_handler(clean_registry: Any) -> None:
    react_agent.install()
    assert registry.handler_for("llm") is react_agent.llm_react_handler


def test_fallback_disabled_for_echo(settings: Settings) -> None:
    assert react_agent.fallback_enabled(settings) is False


def test_fallback_enabled_for_real_provider(settings: Settings) -> None:
    real = settings.model_copy(update={"default_provider": "openrouter"})
    assert react_agent.fallback_enabled(real) is True


def test_handler_requires_factory(settings: Settings) -> None:
    with pytest.raises(ValueError, match="session factory"):
        react_agent.llm_react_handler({"goal": "x"}, {"settings": settings})


def test_handler_requires_goal(settings: Settings) -> None:
    with pytest.raises(ValueError, match="no task goal"):
        react_agent.llm_react_handler({}, {"settings": settings, "factory": object()})


def test_llm_handler_runs_react_loop_with_tool(
    factory: Any, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full loop: model calls file_write via a tool fence, then answers."""
    target = tmp_path / "loop_note.txt"
    first_turn = (
        "I will write the file now.\n\n"
        "```tool:file_write\n"
        f'{{"path": "{target}", "content": "hello from the loop"}}\n'
        "```\n"
    )

    class _StubRouter:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, _factory: Any, model_id: str, prompt: str, **_kw: Any) -> Any:
            self.calls += 1
            if self.calls == 1:
                output = first_turn
            else:
                assert "<tool_result" in prompt  # result fed back to the model
                output = "All done — the file is written."
            return SimpleNamespace(
                ok=True,
                output=output,
                error=None,
                tokens_in=len(prompt) // 4,
                tokens_out=len(output) // 4,
                tokens_cached=0,
                tool_calls=None,
            )

    stub = _StubRouter()
    monkeypatch.setattr(react_agent, "_build_router", lambda _s, _b: stub)

    result = react_agent.llm_react_handler(
        {"goal": "write the loop note"}, _context(factory, settings)
    )

    assert result["output"] == "All done — the file is written."
    assert result["stopped"] == "done"
    assert result["tool_calls"] == 1
    assert result["iterations"] == 2
    assert stub.calls == 2
    assert target.read_text(encoding="utf-8") == "hello from the loop"
    # Memory outcome persisted to the vault.
    notes = list((tmp_path / "vault").rglob("*.md"))
    assert any("write the loop note" in n.read_text(encoding="utf-8") for n in notes)
    # Tool + outcome events recorded in SQLite.
    with session_scope(factory) as db:
        types = {row.type for row in db.query(EventRow).all()}
    # Canonical event taxonomy: tool.started / tool.completed (the former
    # tool.called / tool.result names were never part of the catalog).
    assert "tool.started" in types
    assert "tool.completed" in types


def test_llm_handler_survives_model_failure(
    factory: Any, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing provider call degrades to stopped=error, never raises."""

    class _DeadRouter:
        def invoke(self, *_a: Any, **_kw: Any) -> Any:
            return SimpleNamespace(
                ok=False,
                output=None,
                error="provider unreachable",
                tokens_in=None,
                tokens_out=None,
                tokens_cached=None,
                tool_calls=None,
            )

    monkeypatch.setattr(react_agent, "_build_router", lambda _s, _b: _DeadRouter())
    result = react_agent.llm_react_handler({"goal": "impossible"}, _context(factory, settings))
    assert result["stopped"] == "error"
    assert "provider unreachable" in result["output"]
