"""Telegram gateway (optional communication channel).

Bridges a Telegram bot to the agent system so approved users can submit
goals, inspect status, and approve/deny tasks directly from Telegram.

Two transports are supported (auto-detected from config):
- Long-polling (dev): `TELEGRAM_WEBHOOK_SECRET` empty -> bot uses
  `getUpdates` polling in a background thread.
- Webhook (prod): `TELEGRAM_WEBHOOK_SECRET` set -> FastAPI webhook endpoint
  posts updates in via `handle_update`, and we register the webhook URL with
  the Telegram API.

Auth: only chat ids listed in `TELEGRAM_ALLOWED_CHAT_IDS` can interact.
Everything else is ignored. All bot calls go through a single `httpx.AsyncClient`.

Creating sessions and deciding approvals reuses the same domain services as
the REST API so behavior stays consistent across channels.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import httpx

from agent_system.config import Settings
from agent_system.services.orchestrator import Supervisor
from agent_system.services.permissions import Decision, PermissionGate, Policy, Risk

_API_BASE = "https://api.telegram.org/bot{token}"


def _parse_risk(raw: str) -> Risk:
    return Risk(raw.strip().upper())


class TelegramService:
    """Stateful Telegram gateway.

    ``transport`` is ``"polling"`` or ``"webhook"``. In polling mode a
    daemon thread drives ``_poll_loop``; in webhook mode ``handle_update``
    is called by the API router.
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: Any,
        gate: PermissionGate,
        bus: Any,
    ) -> None:
        self._settings = settings
        self._factory = session_factory
        self._gate = gate
        self._bus = bus
        self._supervisor = Supervisor(bus)
        self._allowed = settings.allowed_chat_ids
        self._token = settings.telegram_bot_token
        self._client: httpx.AsyncClient | None = None
        self._poll_task: asyncio.Task[Any] | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = threading.Event()
        self._last_update_id = 0

    # -- lifecycle ----------------------------------------------------------

    def is_configured(self) -> bool:
        return bool(self._token)

    @property
    def transport(self) -> str:
        if self._settings.telegram_webhook_secret:
            return "webhook"
        return "polling"

    async def start(self) -> None:
        """Begin listening. In polling mode spawns a background thread + loop."""
        if not self.is_configured():
            return
        self._client = httpx.AsyncClient(timeout=30.0)
        if self.transport == "polling":
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._run_polling_loop, args=(self._loop,), daemon=True
            )
            self._thread.start()

    def _run_polling_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        self._poll_task = loop.create_task(self._poll_loop())
        try:
            loop.run_forever()
        finally:
            loop.close()

    async def stop(self) -> None:
        self._stop.set()
        if self._poll_task is not None:
            self._poll_task.cancel()
        if self._thread is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._client is not None:
            await self._client.aclose()

    # -- polling ------------------------------------------------------------

    async def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                updates = await self._get_updates()
                for update in updates:
                    await self.handle_update(update)
            except asyncio.CancelledError:
                break
            except Exception:
                pass
            await asyncio.sleep(1.0)

    async def _get_updates(self) -> list[dict[str, Any]]:
        assert self._client is not None
        resp = await self._client.post(
            _API_BASE.format(token=self._token) + "/getUpdates",
            json={
                "offset": self._last_update_id + 1,
                "timeout": 0,
                "allowed_updates": ["message", "callback_query"],
            },
        )
        data = resp.json()
        if not data.get("ok"):
            return []
        results = list(data.get("result", []) or [])
        if results:
            self._last_update_id = max(self._last_update_id, int(results[-1]["update_id"]))
        return results

    # -- update dispatch ----------------------------------------------------

    async def handle_update(self, update: dict[str, Any]) -> None:
        """Process a single raw Telegram update (shared by polling + webhook)."""
        if not self.is_configured():
            return
        message = update.get("message")
        if message:
            await self._handle_message(message)
            return
        callback = update.get("callback_query")
        if callback:
            await self._handle_callback(callback)

    async def _handle_message(self, message: dict[str, Any]) -> None:
        from_chat = message.get("chat", {}).get("id")
        text = (message.get("text") or "").strip()
        if from_chat is None or not self._is_authorized(from_chat):
            return
        if not text:
            return
        await self._dispatch_command(from_chat, text)

    async def _handle_callback(self, callback: dict[str, Any]) -> None:
        from_chat = callback.get("from", {}).get("id")
        message = callback.get("message", {})
        chat_id = message.get("chat", {}).get("id") or from_chat
        data = callback.get("data") or ""
        if chat_id is None or not self._is_authorized(chat_id):
            return
        if data.startswith("approve:"):
            approval_id = data.split(":", 1)[1]
            await self._decide(chat_id, approval_id, approve=True)
        elif data.startswith("deny:"):
            approval_id = data.split(":", 1)[1]
            await self._decide(chat_id, approval_id, approve=False)

    def _is_authorized(self, chat_id: int) -> bool:
        return chat_id in self._allowed

    # -- command routing ----------------------------------------------------

    async def _dispatch_command(self, chat_id: int, text: str) -> None:
        lower = text.lower().split()
        cmd = lower[0]
        args = text.split()[1:] if len(text.split()) > 1 else []

        if cmd == "/start":
            await self._send(chat_id, "Bob Agent connected.\nSend a goal or use /help.")
        elif cmd == "/help":
            await self._send(
                chat_id,
                "Commands:\n"
                "/start - connect\n"
                "/help - this help\n"
                "/status - summarize active tasks\n"
                "/approve <approval_id> - approve a task\n"
                "/deny <approval_id> - deny a task\n"
                "/cancel <task_id> - cancel a task\n"
                "/retry <task_id> - re-queue a failed task and run it\n"
                "or just send a goal to start a session",
            )
        elif cmd == "/status":
            await self._status(chat_id)
        elif cmd == "/approve" and args:
            await self._decide(chat_id, args[0], approve=True)
        elif cmd == "/deny" and args:
            await self._decide(chat_id, args[0], approve=False)
        elif cmd == "/cancel" and args:
            await self._cancel_task(chat_id, args[0])
        elif cmd == "/retry" and args:
            await self._retry_task(chat_id, args[0])
        else:
            await self._create_session(chat_id, text)

    # -- actions ------------------------------------------------------------

    async def _create_session(self, chat_id: int, goal: str) -> None:
        try:
            session_id = self._supervisor.create_session(self._factory, goal)
        except Exception as exc:
            await self._send(chat_id, f"Failed to create session: {exc}")
            return
        await self._send(
            chat_id,
            f"Session created: {session_id}\nGoal: {goal[:200]}",
        )
        # Cloud (CLOUD_INLINE_RUN): no RQ worker exists, so drive the
        # session in-process in a background thread (webhook must return
        # fast — Telegram retries slow responses). Local dev keeps this
        # off; `make start` runs the real worker instead.
        if bool(getattr(self._settings, "cloud_inline_run", False)):
            thread = threading.Thread(
                target=self._drive_and_report,
                args=(chat_id, session_id),
                daemon=True,
            )
            thread.start()

    def _drive_and_report(self, chat_id: int, session_id: str) -> None:
        """Drive one session in-process, then report the outcome (sync)."""
        try:
            from agent_system.services.cloud import drive_session

            summary = drive_session(self._factory, self._bus, session_id)
        except Exception as exc:
            self._send_sync(chat_id, f"Session {session_id} failed to run: {exc}")
            return
        total = summary["total"]
        ok = summary["succeeded"]
        failed = summary["failed"]
        if failed:
            ids = ", ".join(summary["failed_task_ids"][:5])
            self._send_sync(
                chat_id,
                f"Session {session_id}: {ok}/{total} tasks succeeded, "
                f"{failed} failed ({ids}).\n"
                f"Approve any pending action with /approve <id>, then /retry <task_id>.",
            )
        else:
            self._send_sync(chat_id, f"Session {session_id}: all {total} task(s) succeeded.")

    def _send_sync(self, chat_id: int, text: str) -> None:
        """Send a Telegram message from a background thread (own client)."""
        if not self.is_configured():
            return
        try:
            import httpx as _httpx

            resp = _httpx.Client(timeout=30.0).post(
                _API_BASE.format(token=self._token) + "/sendMessage",
                json={"chat_id": chat_id, "text": text[:4000]},
            )
            resp.raise_for_status()
        except Exception:
            pass

    async def _retry_task(self, chat_id: int, task_id: str) -> None:
        """Explicit retry FAILED -> QUEUED, then drive again in cloud mode."""
        try:
            from agent_system.services.cloud import retry_task_queued

            session_id = retry_task_queued(self._factory, self._bus, task_id)
        except LookupError:
            await self._send(chat_id, f"Unknown task: {task_id}")
            return
        except Exception as exc:
            await self._send(chat_id, f"Task {task_id} not retryable: {exc}")
            return
        await self._send(chat_id, f"Task {task_id} re-queued.")
        if bool(getattr(self._settings, "cloud_inline_run", False)):
            thread = threading.Thread(
                target=self._drive_and_report,
                args=(chat_id, session_id),
                daemon=True,
            )
            thread.start()

    async def _status(self, chat_id: int) -> None:
        try:
            from agent_system.infra.db import session_scope
            from agent_system.infra.models import Session, Task

            with session_scope(self._factory) as db:
                sessions = db.query(Session).order_by(Session.created_at.desc()).limit(5).all()
                if not sessions:
                    await self._send(chat_id, "No sessions yet.")
                    return
                lines = []
                for s in sessions:
                    tasks = (
                        db.query(Task)
                        .filter_by(session_id=s.id)
                        .order_by(Task.created_at.desc())
                        .limit(5)
                        .all()
                    )
                    line = f"• {s.goal[:60]} ({s.id})\n"
                    for t in tasks:
                        line += f"    - {t.title[:40]} [{t.state}] {t.id}\n"
                    lines.append(line.rstrip())
                await self._send(chat_id, "\n".join(lines))
        except Exception as exc:
            await self._send(chat_id, f"Status error: {exc}")

    async def _decide(self, chat_id: int, approval_id: str, approve: bool) -> None:
        record = self._gate.get(approval_id)
        if record is None:
            await self._send(chat_id, f"Unknown approval id: {approval_id}")
            return
        try:
            decided = self._gate.decide(
                approval_id,
                approve=approve,
                policy=Policy.ALLOW_ONCE,
                decided_by=f"telegram:{chat_id}",
            )
        except Exception as exc:
            await self._send(chat_id, f"Decision failed: {exc}")
            return
        verdict = "APPROVED" if approve else "DENIED"
        if decided.decision == Decision.PENDING:
            await self._send(chat_id, "Approval is no longer pending (expired or already decided).")
            return
        await self._send(
            chat_id,
            f"Approval {approval_id} -> {verdict}\nAction: {record.requested_action}",
        )

    async def _cancel_task(self, chat_id: int, task_id: str) -> None:
        try:
            from agent_system.services.orchestrator import Orchestrator

            orch = Orchestrator(self._bus)
            ok = orch.cancel_task(self._factory, task_id)
        except Exception as exc:
            await self._send(chat_id, f"Cancel failed: {exc}")
            return
        if ok:
            await self._send(chat_id, f"Cancelled task {task_id}")
        else:
            await self._send(chat_id, f"Task {task_id} not cancellable (not found / not queued).")

    # -- sends --------------------------------------------------------------

    async def send_message(self, chat_id: int, text: str) -> None:
        await self._send(chat_id, text)

    async def send_notification(self, chat_id: int, text: str) -> None:
        """Fire-and-forget notification to a single approved chat."""
        if not self._is_authorized(chat_id):
            return
        await self._send(chat_id, text)

    async def broadcast(self, text: str, reply_markup: dict[str, Any] | None = None) -> None:
        """Send to every approved chat id (used for approval requests)."""
        for chat_id in self._allowed:
            try:
                await self._send(chat_id, text, reply_markup=reply_markup)
            except Exception:
                continue

    async def _send(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        if not self.is_configured():
            return
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            await self._client.post(
                _API_BASE.format(token=self._token) + "/sendMessage",
                json=payload,
            )
        except httpx.HTTPError:
            pass

    async def send_approval_request(self, record: Any) -> None:
        """Push an approval prompt with inline approve/deny buttons to everyone."""
        text = (
            f"Approval required\n"
            f"Action: {record.requested_action}\n"
            f"Risk: {record.risk.value if hasattr(record.risk, 'value') else record.risk}\n"
            f"Scope: {record.scope}\n"
            f"ID: {record.approval_id}"
        )
        markup = {
            "inline_keyboard": [
                [
                    {"text": "Approve", "callback_data": f"approve:{record.approval_id}"},
                    {"text": "Deny", "callback_data": f"deny:{record.approval_id}"},
                ]
            ]
        }
        await self.broadcast(text, reply_markup=markup)
