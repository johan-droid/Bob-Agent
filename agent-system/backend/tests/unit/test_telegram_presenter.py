"""Unit tests for Telegram presentation layer and progress presenter."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agent_system.domain.events import Event
from agent_system.infra.event_bus import EventBus
from agent_system.services.telegram_presenter import (
    TelegramProgressPresenter,
    map_tool_to_progress,
)


def test_map_tool_to_progress() -> None:
    assert map_tool_to_progress("search_web") == "🔎 Searching the web..."
    assert map_tool_to_progress("git_status") == "💻 Looking at the code..."
    assert map_tool_to_progress("bash_exec") == "⚙️ Running it now..."
    assert map_tool_to_progress("other_tool") == "🛠️ Working on it..."


def test_presenter_subscribes_and_enqueues_progress() -> None:
    bus = EventBus()
    outbox = MagicMock()
    factory = MagicMock()

    # No bot token: start() must fall back to enqueuing a typing row.
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

    # Tool started event creates the ONE stable progress message
    outbox.reset_mock()
    outbox.enqueue.return_value = "out_123"
    # Deterministic throttle clock (real events arrive >1.5s apart).
    with patch("agent_system.services.telegram_presenter.time.monotonic", return_value=100.0):
        bus.emit(
            Event(
                type="tool.started",
                session_id="s1",
                payload={"tool": "search_google"},
            ),
            None,
        )
    assert outbox.enqueue.called
    assert "Searching the web" in outbox.enqueue.call_args[1]["text"]
    assert presenter.progress_outbox_id == "out_123"

    # After binding the captured Telegram message id, later stages EDIT the
    # same message (kind=progress_edit + edit_message_id) — no message spam.
    presenter.bind_telegram_message_id(777)
    outbox.reset_mock()
    with patch("agent_system.services.telegram_presenter.time.monotonic", return_value=102.0):
        bus.emit(
            Event(
                type="tool.started",
                session_id="s1",
                payload={"tool": "git_status"},
            ),
            None,
        )
    assert outbox.enqueue.called
    assert outbox.enqueue.call_args[1]["kind"] == "progress_edit"
    assert outbox.enqueue.call_args[1]["edit_message_id"] == 777
    assert "Looking at the code" in outbox.enqueue.call_args[1]["text"]

    presenter.stop()


def test_presenter_sends_typing_directly_with_bot_token() -> None:
    from unittest.mock import patch

    bus = EventBus()
    outbox = MagicMock()

    presenter = TelegramProgressPresenter(
        factory=MagicMock(),
        outbox=outbox,
        chat_id=123,
        session_id="s1",
        bus=bus,
        bot_token="real:token",
    )
    with patch("agent_system.services.telegram.send_chat_action_sync") as typing_mock:
        presenter.start()

    typing_mock.assert_called_once_with("real:token", 123)
    # Direct typing send: no outbox row needed
    assert not outbox.enqueue.called
