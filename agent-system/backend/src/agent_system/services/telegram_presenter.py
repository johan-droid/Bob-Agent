"""Telegram Presentation Layer & Progress Presenter (UX Polish).

Listens to agent execution events (request.received, model.started, tool.started,
tool.completed, model.first_token, task.completed, task.failed) and drives a
single editable Telegram progress message with concise status indicators.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from agent_system.domain.events import Event
from agent_system.infra.event_bus import EventBus

logger = logging.getLogger(__name__)

UPDATE_THROTTLE_SECONDS = 1.5

_INTERNAL_ID_PATTERNS = [
    r"Task accepted:\s*[a-zA-Z0-9_-]+",
    r"Task registered:\s*[a-zA-Z0-9_-]+",
    r"Session\s+[a-zA-Z0-9_-]+:\s*",
    r"task_[0-9A-Za-z_]+",
    r"session_[0-9A-Za-z_]+",
    r"run_[0-9A-Za-z_]+",
    r"tgm_[0-9A-Za-z_]+",
    r"attempt_[0-9A-Za-z_]+",
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
    r"Worker assigned:[^\n]*",
]


def build_task_ack(goal: str, req_type: str = "TOOL_TASK") -> str:
    """Generate a context-aware initial task acknowledgement based on classification and goal."""
    lower = (goal or "").lower()
    if "deploy" in lower:
        return "Checking deployment details and preparing the task..."
    if "debug" in lower or "fix" in lower or "bug" in lower:
        return "Investigating the issue and debugging..."
    if req_type == "RESEARCH_TASK" or "search" in lower or "research" in lower or "find" in lower:
        return "Gathering information and researching..."
    if req_type == "CODING_TASK" or "code" in lower or "script" in lower or "refactor" in lower:
        return "Working on the code implementation..."
    if req_type == "LONG_RUNNING_TASK" or "batch" in lower or "crawl" in lower:
        return "Initiating the process..."
    return "Starting work on your request..."


def format_model_footer(
    provider: str | None, model_id: str | None, latency_s: float | None = None
) -> str:
    """Format model transparency footer line: ↳ groq · llama-3.3-70b-versatile · 0.8s"""
    p = (provider or "groq").strip().lower()
    m = (model_id or "default").strip()
    if "/" in m:
        m = m.split("/")[-1]
    if latency_s is not None and latency_s > 0:
        return f"↳ {p} · {m} · {latency_s:.1f}s"
    return f"↳ {p} · {m}"


def sanitize_telegram_message(text: str) -> str:
    """Strip all internal IDs, tracebacks, and technical noise from Telegram user messages."""
    if not text:
        return ""
    cleaned = text
    if "Traceback (most recent call last):" in cleaned:
        return "Sorry, I ran into an issue while fulfilling your request."

    for pattern in _INTERNAL_ID_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned)

    lines = [line.strip() for line in cleaned.split("\n")]
    result = "\n".join(line for line in lines if line)
    return result or text


def load_chat_history(factory: Any, chat_id: int | str, limit: int = 10) -> list[dict[str, str]]:
    """Load recent Telegram chat history for context continuity."""
    if factory is None or not chat_id:
        return []
    try:
        from agent_system.infra.db import session_scope
        from agent_system.infra.models import TelegramChatHistory

        with session_scope(factory) as db:
            rows = (
                db.query(TelegramChatHistory)
                .filter(TelegramChatHistory.chat_id == str(chat_id))
                .order_by(TelegramChatHistory.created_at.desc())
                .limit(limit)
                .all()
            )
            return [{"role": r.role, "content": r.content} for r in reversed(rows)]
    except Exception:
        return []


def save_chat_message(factory: Any, chat_id: int | str, role: str, content: str) -> None:
    """Save a single message to Telegram chat history."""
    if factory is None or not chat_id or not content:
        return
    try:
        from agent_system.domain import ids
        from agent_system.infra.db import session_scope
        from agent_system.infra.models import TelegramChatHistory

        with session_scope(factory) as db:
            db.add(
                TelegramChatHistory(
                    id=ids.new_id("tch"),
                    chat_id=str(chat_id),
                    role=role,
                    content=content,
                )
            )
    except Exception:
        pass


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
        # No initial message here: progress is reported from real
        # model/tool events below, and empty texts are rejected by the
        # outbox (Telegram 400s). The typing animation is sent via
        # sendChatAction by the caller before the drive starts.

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


__all__ = [
    "TelegramProgressPresenter",
    "build_task_ack",
    "format_model_footer",
    "load_chat_history",
    "map_tool_to_progress",
    "sanitize_telegram_message",
    "save_chat_message",
]
