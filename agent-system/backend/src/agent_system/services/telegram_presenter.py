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
    """Generate a context-aware initial task acknowledgement based on classification and goal.

    NOTE: Bob sends Telegram messages with NO parse_mode (formatting-injection
    safe), so keep these plain text + emoji only — never Markdown/HTML markup.
    """
    lower = (goal or "").lower()
    if "deploy" in lower:
        return "🚀 On it! Checking deployment details and preparing the task..."
    if "debug" in lower or "fix" in lower or "bug" in lower:
        return "🐛 Got it! Investigating the issue and debugging..."
    if req_type == "RESEARCH_TASK" or "search" in lower or "research" in lower or "find" in lower:
        return "🔎 On it! Gathering information and researching..."
    if req_type == "CODING_TASK" or "code" in lower or "script" in lower or "refactor" in lower:
        return "💻 Got it! Working on the code..."
    if req_type == "LONG_RUNNING_TASK" or "batch" in lower or "crawl" in lower:
        return "⏳ On it! Kicking off the long job — I'll keep you posted..."
    return "⚡️ Got it! Starting work on your request..."


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
    """Map a tool name to a friendly concise progress status (emoji + plain text)."""
    name = (tool_name or "").lower()
    if any(s in name for w in ("search", "google", "web", "find", "browse") for s in (w,)):
        return "🔎 Searching the web..."
    if any(s in name for w in ("memory", "recall", "vault", "note") for s in (w,)):
        return "🧠 Checking memory..."
    if any(s in name for w in ("git", "inspect", "read", "file", "code", "python") for s in (w,)):
        return "💻 Looking at the code..."
    if any(s in name for w in ("bash", "shell", "exec", "terminal", "run") for s in (w,)):
        return "⚙️ Running it now..."
    if any(s in name for w in ("mail", "gmail", "email", "send") for s in (w,)):
        return "📧 Handling email..."
    return "🛠️ Working on it..."


class TelegramProgressPresenter:
    """Subscribes to EventBus for a session and manages single-message progress UX.

    Lifecycle contract (Telegram realtime UX):

    1. ``start()`` fires a ``typing`` chat action IMMEDIATELY so the user sees
       life within milliseconds.
    2. The first real progress event creates ONE stable Telegram message and
       records its outbox id; all later stages EDIT that same message
       (``kind="progress_edit"`` + ``edit_message_id``) — no message spam.
    3. After the first delivery is drained, the caller feeds the captured
       Telegram ``message_id`` back via :meth:`bind_telegram_message_id` so
       subsequent edits target the actual user-visible message.
    """

    def __init__(
        self,
        factory: Any,
        outbox: Any,
        chat_id: int,
        session_id: str,
        bus: EventBus,
        bot_token: str | None = None,
    ) -> None:
        self._factory = factory
        self._outbox = outbox
        self._chat_id = chat_id
        self._session_id = session_id
        self._bus = bus
        self._bot_token = bot_token
        self._active = False
        self._progress_outbox_id: str | None = None
        self._edit_message_id: int | None = None
        self._last_update_ts: float = 0.0
        self._last_text: str = ""
        #: Last emergency-fallback notice delivered (dedup; never repeated).
        self._last_fallback_notice: str = ""

    def start(self) -> None:
        if self._active:
            return
        self._active = True
        # P0: the typing animation must fire the moment work starts. When the
        # bot token is available, send it directly (synchronous, ~50ms); when
        # it is not, fall back to enqueuing a typing row so the outbox drain
        # still produces the animation.
        if self._bot_token:
            from agent_system.services.telegram import send_chat_action_sync

            send_chat_action_sync(self._bot_token, self._chat_id)
        elif self._outbox is not None:
            self._outbox.enqueue(kind="typing", chat_id=self._chat_id, text="typing")
        self._bus.subscribe("tool.started", self._on_event)
        self._bus.subscribe("tool.completed", self._on_event)
        self._bus.subscribe("model.started", self._on_event)
        self._bus.subscribe("model.token", self._on_event)
        self._bus.subscribe("task.failed", self._on_event)
        # Ollama Cloud-first runtime: the one model decision for this task,
        # and a one-off notice when the emergency layer engages.
        self._bus.subscribe("inference.model_selected", self._on_event)
        self._bus.subscribe("inference.fallback_activated", self._on_event)
        self._bus.subscribe("inference.fallback_notice", self._on_event)

    def stop(self) -> None:
        self._active = False

    def bind_telegram_message_id(self, telegram_message_id: int | None) -> None:
        """Bind the user-visible Telegram message id that edits must target.

        Called by the executor after the first progress row is drained: the
        outbox captured the ``message_id`` from the sendMessage response, and
        this presenter needs it to route later stages as in-place edits.
        """
        if telegram_message_id is not None:
            self._edit_message_id = int(telegram_message_id)

    @property
    def progress_outbox_id(self) -> str | None:
        """Outbox row id of the stable progress message (None until created)."""
        return self._progress_outbox_id

    def _on_event(self, event: Event) -> None:
        if not self._active:
            return
        if event.session_id and event.session_id != self._session_id:
            return
        if event.type in ("inference.fallback_activated", "inference.fallback_notice"):
            self._on_fallback(event)
            return

        now = time.monotonic()
        text = ""

        if event.type == "inference.model_selected":
            label = str(event.payload.get("label") or "").strip()
            text = f"{label} — getting started..." if label else "🧠 Thinking..."
        elif event.type == "tool.started":
            tool_name = str(event.payload.get("tool") or "")
            text = map_tool_to_progress(tool_name)
        elif event.type == "model.started":
            text = "🧠 Thinking..."
        elif event.type == "model.token":
            text = "✍️ Writing the reply..."
        elif event.type == "tool.completed":
            text = "✅ Tool done — crunching the results..."
        elif event.type == "task.failed":
            text = "😅 Sorry, I hit a snag — please try again."

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
                # In-place edit of the stable progress message (best-effort:
                # the outbox marks failed edits delivered instead of retrying).
                self._outbox.enqueue(
                    kind="progress_edit",
                    chat_id=self._chat_id,
                    text=text,
                    edit_message_id=self._edit_message_id,
                )
            else:
                # First stage: create the ONE stable progress message. Its
                # captured Telegram message id is bound later via
                # bind_telegram_message_id() after the first drain.
                self._progress_outbox_id = self._outbox.enqueue(
                    kind="command_response",
                    chat_id=self._chat_id,
                    text=text,
                )

    def _on_fallback(self, event: Event) -> None:
        """One-off, truthful notice that the emergency layer engaged.

        Never repeated, never an internal id and never a raw provider error:
        the text is the runtime's user-safe notice (``FALLBACK_NOTICE``).
        """
        from agent_system.services.inference_runtime import FALLBACK_NOTICE

        notice = str(event.payload.get("notice") or FALLBACK_NOTICE).strip()
        if not notice or notice == self._last_fallback_notice:
            return
        self._last_fallback_notice = notice
        if self._outbox is not None:
            self._outbox.enqueue(
                kind="notification",
                chat_id=self._chat_id,
                text=notice,
            )


__all__ = [
    "TelegramProgressPresenter",
    "build_task_ack",
    "format_model_footer",
    "load_chat_history",
    "map_tool_to_progress",
    "sanitize_telegram_message",
    "save_chat_message",
]
