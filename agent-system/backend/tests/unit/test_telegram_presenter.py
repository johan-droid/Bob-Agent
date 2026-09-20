"""Unit tests for Telegram presentation layer and progress presenter."""

from __future__ import annotations

from unittest.mock import MagicMock

from agent_system.domain.events import Event
from agent_system.infra.event_bus import EventBus
from agent_system.services.telegram_presenter import (
    TelegramProgressPresenter,
    map_tool_to_progress,
)


def test_map_tool_to_progress() -> None:
    assert map_tool_to_progress("search_web") == "🔎 Searching..."
    assert map_tool_to_progress("git_status") == "💻 Inspecting..."
    assert map_tool_to_progress("bash_exec") == "🛠️ Running a tool..."
    assert map_tool_to_progress("other_tool") == "🛠️ Working..."


def test_presenter_subscribes_and_enqueues_progress() -> None:
    bus = EventBus()
    outbox = MagicMock()
    factory = MagicMock()

    presenter = TelegramProgressPresenter(
        factory=factory,
        outbox=outbox,
        chat_id=123,
        session_id="s1",
        bus=bus,
    )
    presenter.start()

    # Initial typing action enqueued
    assert outbox.enqueue.called
    assert outbox.enqueue.call_args[1]["kind"] == "typing"

    # Tool started event
    outbox.reset_mock()
    bus.emit(
        Event(
            type="tool.started",
            session_id="s1",
            payload={"tool": "search_google"},
        ),
        None,
    )
    assert outbox.enqueue.called
    assert "Searching" in outbox.enqueue.call_args[1]["text"]

    presenter.stop()
