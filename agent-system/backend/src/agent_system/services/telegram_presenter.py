"""Telegram Presentation Layer & Progress Presenter (UX Polish).

Listens to agent execution events (request.received, model.started, tool.started,
tool.completed, model.first_token, task.completed, task.failed) and drives a
single editable Telegram progress message with concise status indicators.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from agent_system.domain.events import Event
from agent_system.infra.event_bus import EventBus

logger = logging.getLogger(__name__)

UPDATE_THROTTLE_SECONDS = 1.5


def map_tool_to_progress(tool_name: str) -> str:
    """Map a tool name to a friendly concise progress status."""
    name = (tool_name or "").lower()
    if any(s in name for w in ("search", "google", "web", "find", "browse") for s in (w,)):
        return "🔎 Searching..."
    if any(s in name for w in ("git", "inspect", "read", "file", "code", "python") for s in (w,)):
        return "💻 Inspecting..."
    if any(s in name for w in ("bash", "shell", "exec", "terminal", "run") for s in (w,)):
        return "🛠️ Running a tool..."
    return "🛠️ Working..."


class TelegramProgressPresenter:
    """Subscribes to EventBus for a session and manages single-message progress UX."""

    def __init__(
        self,
        factory: Any,
        outbox: Any,
        chat_id: int,
        session_id: str,
        bus: EventBus,
    ) -> None:
        self._factory = factory
        self._outbox = outbox
        self._chat_id = chat_id
        self._session_id = session_id
        self._bus = bus
        self._active = False
        self._edit_message_id: int | None = None
        self._last_update_ts: float = 0.0
        self._last_text: str = ""

    def start(self) -> None:
        if self._active:
            return
        self._active = True
        self._bus.subscribe("tool.started", self._on_event)
        self._bus.subscribe("tool.completed", self._on_event)
        self._bus.subscribe("model.started", self._on_event)
        self._bus.subscribe("model.token", self._on_event)
        self._bus.subscribe("task.failed", self._on_event)

        # Enqueue initial typing & working message
        if self._outbox is not None:
            self._outbox.enqueue(kind="typing", chat_id=self._chat_id, text="")

    def stop(self) -> None:
        self._active = False

    def _on_event(self, event: Event) -> None:
        if not self._active:
            return
        if event.session_id and event.session_id != self._session_id:
            return

        now = time.monotonic()
        text = ""

        if event.type == "tool.started":
            tool_name = str(event.payload.get("tool") or "")
            text = map_tool_to_progress(tool_name)
        elif event.type == "model.started":
            text = "🧠 Working..."
        elif event.type == "model.token":
            text = "✍️ Preparing response..."
        elif event.type == "tool.completed":
            text = "🧠 Processing results..."
        elif event.type == "task.failed":
            text = "Sorry, I ran into an issue while fulfilling your request. Please try again."

        if not text or text == self._last_text:
            return

        # Throttle updates to avoid flooding Telegram
        is_failed = event.type == "task.failed"
        if now - self._last_update_ts < UPDATE_THROTTLE_SECONDS and not is_failed:
            return

        self._last_update_ts = now
        self._last_text = text

        if self._outbox is not None:
            if self._edit_message_id is not None:
                self._outbox.enqueue(
                    kind="progress_edit",
                    chat_id=self._chat_id,
                    text=text,
                    edit_message_id=self._edit_message_id,
                )
            else:
                outbox_id = self._outbox.enqueue(
                    kind="command_response",
                    chat_id=self._chat_id,
                    text=text,
                )
                if outbox_id:
                    self._edit_message_id = None  # Updated when message delivered


__all__ = ["TelegramProgressPresenter", "map_tool_to_progress"]
