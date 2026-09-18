"""Telegram gateway (identity-aware cloud transport).

Telegram is a transport layer; Bob Core semantics are shared one-for-one with
the CLI/Desktop (spec §2, §12, §17, §27).

Hardening over the legacy gateway:
- Identity: Telegram user id -> IdentityService -> Bob User + Role. A
  Telegram principal is identity, never authorization (spec §9).
- Dedup: every inbound update is logged to ``telegram_updates`` by primary
  key ``update_id`` BEFORE processing (crash-safe ingest). A retried delivery
  loses the INSERT race, but is only dropped once the row is marked COMPLETED
  (``processed_at`` written after processing succeeds). A retry whose row is
  still ``processed_at IS NULL`` — the crash window — is re-processed, so a
  crash between ingest and completion cannot silently delete a user request
  (at-least-once work, at-most-once completion marker; spec §3, §30).
- Delivery: outbound messages are persisted to the delivery outbox
  (persist-first, retryable, survives restarts) when a DB factory is wired.
  Falls back to direct httpx when no factory is available (tests, local
  single-operator).
- Commands are a typed registry, not a giant if/else (spec §11).

Deployment modes:
- ``local`` (AGENT_IDENTITY_MODE=local, default): backward compatible. The
  chat-id allowlist (TELEGRAM_ALLOWED_CHAT_IDS) gates access; a single
  operator identity is assumed for ownership.
- ``telegram``: multi-user. Every Telegram user id is resolved through the
  IdentityService to a Bob user + role; ownership columns on sessions/tasks
  are enforced; group members only get the capabilities of their role.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.exc import IntegrityError

from agent_system.config import Settings
from agent_system.domain.events import utcnow
from agent_system.infra.db import session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import TelegramUpdate
from agent_system.services.identity import (
    OPERATOR,
    IdentityMode,
    IdentityService,
    Principal,
    Role,
)
from agent_system.services.orchestrator import Supervisor
from agent_system.services.outbox import (
    KIND_APPROVAL,
    KIND_COMMAND_RESPONSE,
    KIND_NOTIFICATION,
    Outbox,
)
from agent_system.services.permissions import (
    ApprovalRecord,
    PermissionGate,
    Policy,
)

_logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}"
_HTTP_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# Command registry (spec §11).


@dataclass
class CommandDef:
    """A typed, self-describing Telegram command."""

    name: str
    description: str
    handler: Callable[..., Awaitable[None]]
    permission: str | None = None
    requires_args: bool = False
    variadic: bool = False


# ---------------------------------------------------------------------------
# Service.


class TelegramService:
    """Stateful, identity-aware Telegram gateway.

    ``transport`` is ``polling`` (dev, no webhook secret) or ``webhook``
    (prod). In polling mode a daemon thread drives ``getUpdates``; in webhook
    mode ``handle_update`` is called by the API router.

    Both transports converge on the same ``handle_update`` seam, which
    de-duplicates via the ``telegram_updates`` ledger, resolves the principal,
    routes commands, and enqueues deliveries to the outbox.
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: Any,
        gate: PermissionGate,
        bus: EventBus,
    ) -> None:
        self._settings = settings
        self._factory = session_factory
        self._gate = gate
        self._bus = bus
        self._supervisor = Supervisor(bus)
        # Identity layer — None in local mode (backward compat).
        self._identity = (
            IdentityService(session_factory, settings)
            if getattr(settings, "agent_identity_mode", "local") == IdentityMode.TELEGRAM.value
            else None
        )
        # Outbox — None when no DB factory (tests, ephemeral local).
        self._outbox = Outbox(session_factory, settings) if session_factory else None
        # Backward-compat chat-id allowlist for local / pre-identity mode.
        self._allowed = settings.allowed_chat_ids
        self._token = settings.telegram_bot_token
        self._client: httpx.AsyncClient | None = None
        # In-memory dedup fallback when no DB factory is wired.
        self._seen_updates: set[int] = set()
        # Polling lifecycle.
        self._poll_task: asyncio.Task[Any] | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = threading.Event()
        self._update_offset: int = 0
        # Command registry.
        self._commands: dict[str, CommandDef] = {}
        self._register_commands()

    # -- command registry ---------------------------------------------------

    def _register_commands(self) -> None:
        self._commands["/start"] = CommandDef("/start", "Connect Bob to this chat", self._cmd_start)
        self._commands["/help"] = CommandDef("/help", "Show available commands", self._cmd_help)
        self._commands["/status"] = CommandDef(
            "/status", "Summarize active sessions and tasks", self._cmd_status
        )
        self._commands["/cancel"] = CommandDef(
            "/cancel",
            "Cancel a task",
            self._cmd_cancel,
            permission="task.cancel",
            requires_args=True,
        )
        self._commands["/retry"] = CommandDef(
            "/retry",
            "Re-queue a failed task",
            self._cmd_retry,
            permission="task.cancel",
            requires_args=True,
        )
        self._commands["/approve"] = CommandDef(
            "/approve",
            "Grant a pending approval",
            self._cmd_approve,
            permission="approval.decide",
            requires_args=True,
        )
        self._commands["/deny"] = CommandDef(
            "/deny",
            "Deny a pending approval",
            self._cmd_deny,
            permission="approval.decide",
            requires_args=True,
        )

    @property
    def commands(self) -> dict[str, CommandDef]:
        """Read-only view of registered commands (for the /help and status UX)."""
        return dict(self._commands)

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
        self._client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
        if self.transport == "polling":
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._run_polling_loop, args=(self._loop,), daemon=True
            )
            self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _run_polling_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        self._poll_task = loop.create_task(self._poll_loop())
        try:
            loop.run_forever()
        finally:
            loop.close()

    async def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                updates = await self._get_updates()
                for update in updates:
                    await self.handle_update(update)
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("telegram poll loop error")
            await asyncio.sleep(max(0, self._settings.outbox_poll_seconds or 1.0))

    async def _get_updates(self) -> list[dict[str, Any]]:
        """Long-poll ``getUpdates``; updates are de-duped in handle_update."""
        if self._client is None or not self._token:
            return []
        resp = await self._client.get(
            _API_BASE.format(token=self._token) + "/getUpdates",
            params={
                "offset": self._update_offset + 1,
                "timeout": 30,
                "allowed_updates": ["message", "callback_query"],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        updates: list[dict[str, Any]] = data.get("result", [])
        if updates:
            self._update_offset = max(u["update_id"] for u in updates)
        return updates

    # -- identity & authorization -------------------------------------------

    def _is_authorized(self, chat_id: int) -> bool:
        """Backward-compatible local-mode check (chat-id allowlist).

        In identity mode, authorization is resolved from the Telegram user id
        via :meth:`_resolve_principal`. Kept so existing callers and tests that
        pass a raw chat id keep working.
        """
        if self._identity is not None:
            return True  # principal resolved elsewhere; check is identity-based
        return chat_id in self._allowed

    def _resolve_principal(
        self, chat_id: int, from_user: dict[str, Any] | None
    ) -> Principal | None:
        """Resolve a Telegram update originator to a principal.

        - Local mode: chat id must be on the allowlist -> OPERATOR.
        - Identity mode: Telegram user id -> IdentityService -> Principal.
        """
        if self._identity is None:
            if chat_id in self._allowed:
                return OPERATOR
            return None
        tid = str(from_user["id"]) if from_user else None
        if tid is None:
            return None
        return self._identity.resolve(tid, chat_id)

    def _authorized_chat_ids(self) -> list[int]:
        """All chat ids that should receive an approval broadcast.

        Local mode: the allowlist. Identity mode: all active, non-blocked
        TelegramAccount chat ids resolved from the DB.
        """
        if self._identity is None:
            return sorted(self._allowed)
        from agent_system.infra.models import TelegramAccount, User

        with session_scope(self._factory) as db:
            rows = (
                db.query(TelegramAccount)
                .join(User, TelegramAccount.user_id == User.id)
                .filter(
                    TelegramAccount.role != Role.BLOCKED.value,
                    User.is_active.is_(True),
                    TelegramAccount.chat_id.isnot(None),
                )
                .all()
            )
        return [int(r.chat_id) for r in rows if r.chat_id]

    # -- inbound: update handling -------------------------------------------

    async def handle_update(self, update: dict[str, Any]) -> None:
        """Process a single raw Telegram update (shared by polling + webhook).

        Crash-safe ingest: the update is logged to the ledger BEFORE any side
        effect, so a re-delivered webhook can be de-duplicated. The ledger row
        is marked COMPLETED (``processed_at``) only after processing finishes;
        a crash mid-processing leaves the row unprocessed, so Telegram's retry
        re-processes it instead of dropping the user's request.
        """
        if not self.is_configured():
            return
        # Idempotency gate: log the update BEFORE any side effects.
        if not self._log_update(update):
            # Already completed (at-most-once) — skip silently. A crash-window
            # retry (row exists but `processed_at IS NULL`) is reclaimed here
            # and processed now.
            return
        update_id = update.get("update_id")
        message = update.get("message")
        callback = update.get("callback_query")
        if message:
            await self._handle_message(message)
        elif callback:
            await self._handle_callback(callback)
        # No message/callback payload still counts as handled (no-op).
        if update_id is not None:
            self._mark_processed(int(update_id))

    def _log_update(self, update: dict[str, Any]) -> bool:
        """Record the update in the ingest ledger. Returns True to process.

        A brand-new update inserts the ledger row (durable payload buffer) and
        returns True. A duplicate is only skipped when the previous ingestion
        reached COMPLETED (``processed_at`` set). A duplicate whose row is
        still ``processed_at IS NULL`` means the first attempt ingested the
        update but never finished — the retry refreshes the payload buffer and
        returns True so the request is processed at-least-once, never dropped.
        """
        update_id = update.get("update_id")
        if update_id is None:
            return True
        key = int(update_id)
        if self._factory is None:
            # In-memory fallback (no DB factory): de-dup within the process.
            if key in self._seen_updates:
                return False
            self._seen_updates.add(key)
            return True
        from_chat = update.get("message", {}).get("chat", {}).get("id")
        from_user = (
            (update.get("message") or {}).get("from")
            or (update.get("callback_query") or {}).get("from")
            or {}
        )
        account_id = str(from_user.get("id")) if from_user.get("id") else None
        with session_scope(self._factory) as db:
            row = TelegramUpdate(
                update_id=key,
                account_id=account_id,
                chat_id=str(from_chat) if from_chat else None,
                payload_json=update,
            )
            db.add(row)
            try:
                db.flush()
            except IntegrityError:
                db.rollback()
                existing = db.get(TelegramUpdate, key)
                if existing is None or existing.processed_at is not None:
                    return False
                # Crash-window retry: re-deliver while the previous attempt
                # never reached COMPLETED.
                existing.account_id = existing.account_id or account_id
                existing.chat_id = existing.chat_id or (str(from_chat) if from_chat else None)
                existing.payload_json = update
                return True
            return True

    def _mark_processed(self, update_id: int) -> None:
        """Write the durable COMPLETED marker for a handled update.

        A row with ``processed_at`` set is never re-processed on a later
        delivery. The marker is written only after processing succeeded, so the
        ``processed_at IS NULL`` state remains the reclaimable crash window.
        """
        if self._factory is None:
            return
        with session_scope(self._factory) as db:
            row = db.get(TelegramUpdate, update_id)
            if row is not None and row.processed_at is None:
                row.processed_at = utcnow()

    async def _handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat", {})
        from_chat = chat.get("id")
        from_user = message.get("from", {})
        text = (message.get("text") or "").strip()
        if from_chat is None:
            return
        principal = self._resolve_principal(from_chat, from_user)
        if principal is None:
            self._audit_denied(from_chat, from_user)
            return
        if not text:
            return
        await self._dispatch_command(principal, from_chat, text)

    async def _handle_callback(self, callback: dict[str, Any]) -> None:
        from_chat = callback.get("message", {}).get("chat", {}).get("id")
        from_user = callback.get("from", {})
        data = callback.get("data") or ""
        chat_id = from_chat or from_user.get("id")
        if chat_id is None:
            return
        principal = self._resolve_principal(chat_id, from_user)
        if principal is None:
            self._audit_denied(chat_id, from_user)
            return
        if data.startswith("approve:"):
            approval_id = data.split(":", 1)[1]
            await self._cmd_approve_run(principal, chat_id, approval_id, approve=True)
        elif data.startswith("deny:"):
            approval_id = data.split(":", 1)[1]
            await self._cmd_approve_run(principal, chat_id, approval_id, approve=False)

    def _audit_denied(self, chat_id: int, from_user: dict[str, Any] | None) -> None:
        """Log a silent denial — no oracle to probing senders (spec §10)."""
        tid = str(from_user.get("id")) if from_user else "unknown"
        _logger.info(
            "telegram update from chat %s (user %s) ignored (not provisioned)",
            chat_id,
            tid,
        )

    # -- command routing ----------------------------------------------------

    async def _dispatch_command(
        self,
        principal: Principal,
        chat_id: int,
        text: str,
    ) -> None:
        lower = text.lower().split()
        cmd_name = lower[0] if lower else ""
        args = text.split()[1:] if len(text.split()) > 1 else []

        cmd = self._commands.get(cmd_name)
        if cmd is None:
            # Natural-language goal -> start a session (spec §12).
            await self._create_session(principal, chat_id, text)
            return
        if cmd.permission is not None and not principal.can(cmd.permission):
            await self._send(chat_id, "You do not have permission for that command.")
            return
        if cmd.requires_args and not args:
            await self._send(chat_id, f"{cmd.name} requires arguments. See /help.")
            return
        try:
            await cmd.handler(principal, chat_id, *args)
        except Exception as exc:
            await self._send(chat_id, f"Command failed: {exc}")

    # -- command handlers ---------------------------------------------------

    async def _cmd_start(self, principal: Principal, chat_id: int, *args: str) -> None:
        await self._send(
            chat_id,
            "Bob Agent connected. Send a goal or use /help.",
            kind=KIND_COMMAND_RESPONSE,
        )

    async def _cmd_help(self, principal: Principal, chat_id: int, *args: str) -> None:
        lines = ["/help — show this help", "/status — active sessions/tasks"]
        if principal.can("task.cancel"):
            lines += ["/cancel <task_id>", "/retry <task_id>"]
        if principal.can("approval.decide"):
            lines += ["/approve <id>", "/deny <id>"]
        lines += ["\nOr just send a goal — Bob will start working on it."]
        await self._send(chat_id, "\n".join(lines), kind=KIND_COMMAND_RESPONSE)

    async def _cmd_status(self, principal: Principal, chat_id: int, *args: str) -> None:
        await self._status(chat_id)

    async def _cmd_cancel(self, principal: Principal, chat_id: int, *args: str) -> None:
        task_id = args[0] if args else ""
        await self._cancel_task(principal, chat_id, task_id)

    async def _cmd_retry(self, principal: Principal, chat_id: int, *args: str) -> None:
        task_id = args[0] if args else ""
        await self._retry_task(principal, chat_id, task_id)

    async def _cmd_approve(self, principal: Principal, chat_id: int, *args: str) -> None:
        approval_id = args[0] if args else ""
        await self._cmd_approve_run(principal, chat_id, approval_id, approve=True)

    async def _cmd_deny(self, principal: Principal, chat_id: int, *args: str) -> None:
        approval_id = args[0] if args else ""
        await self._cmd_approve_run(principal, chat_id, approval_id, approve=False)

    # -- core handlers (kept from legacy, identity-wired) -------------------

    async def _create_session(self, principal: Principal, chat_id: int, goal: str) -> None:
        owner_id = principal.user_id
        try:
            session_id = self._supervisor.create_session(
                self._factory, goal, owner_user_id=owner_id
            )
        except Exception as exc:
            await self._send(chat_id, f"Failed to create session: {exc}")
            return
        # Durable chat<->session mapping: lets the terminal-result relay find
        # this chat after a restart (Telegram Gateway E2E, additive).
        try:
            from agent_system.services.gateway import record_session_chat

            record_session_chat(self._factory, session_id, int(chat_id))
        except Exception:
            _logger.exception("telegram: session-chat mapping failed (session %s)", session_id)
        await self._send(
            chat_id,
            f"Session created: {session_id}\nGoal: {goal[:200]}",
        )
        # Cloud (CLOUD_INLINE_RUN): no RQ worker, so drive in-process.
        # The web request must return fast; Telegram retries slow responses.
        if bool(getattr(self._settings, "cloud_inline_run", False)):
            thread = threading.Thread(
                target=self._drive_and_report,
                args=(chat_id, session_id),
                daemon=True,
            )
            thread.start()

    def _drive_and_report(self, chat_id: int, session_id: str) -> None:
        try:
            from agent_system.services.cloud import drive_session

            summary = drive_session(self._factory, self._bus, session_id)
        except Exception as exc:
            self._send_sync(chat_id, f"Session {session_id} failed: {exc}")
            return
        ok = summary["succeeded"]
        total = summary["total"]
        failed = summary["failed"]
        unfinished = summary.get("unfinished", 0)
        if failed:
            ids = ", ".join(summary["failed_task_ids"][:5])
            self._send_sync(
                chat_id,
                f"Session {session_id}: {ok}/{total} succeeded, {failed} failed ({ids}).",
            )
        elif unfinished:
            self._send_sync(
                chat_id,
                f"Session {session_id}: {ok}/{total} succeeded, "
                f"{unfinished} unfinished (drive safety limit); /retry or ask again.",
            )
        else:
            self._send_sync(chat_id, f"Session {session_id}: all {total} succeeded.")

    def _task_owned_by_another(self, task_id: str, principal: Principal) -> bool:
        """True only when a task is owned by a DIFFERENT user than the principal.

        Object-level isolation for Telegram task control (/cancel, /retry).
        Semantics mirror the approval ownership gate: the guard only fires when
        BOTH sides are identified (task has an owner AND the principal has a
        user id), so local/single-user and legacy ownerless flows are
        unaffected. Mismatched owners are hard-denied with a not-found-style
        message (no oracle).
        """
        uid = getattr(principal, "user_id", None)
        if uid is None or self._factory is None:
            return False
        try:
            from agent_system.infra.db import session_scope
            from agent_system.infra.models import Task as TaskRow

            with session_scope(self._factory) as db:
                row = db.get(TaskRow, task_id)
                if row is None or row.owner_user_id is None:
                    return False
                return row.owner_user_id != str(uid)
        except Exception:
            # Cannot read the task to prove ownership: keep today's behaviour.
            return False

    async def _retry_task(self, principal: Principal, chat_id: int, task_id: str) -> None:
        if self._task_owned_by_another(task_id, principal):
            await self._send(chat_id, f"Task {task_id} not found or not authored by you.")
            return
        try:
            from agent_system.services.cloud import retry_task_queued

            retry_task_queued(self._factory, self._bus, task_id)
        except Exception as exc:
            await self._send(chat_id, f"Retry failed: {exc}")
            return
        await self._send(chat_id, f"Re-queued task {task_id}")

    async def _status(self, chat_id: int) -> None:
        try:
            summary = self._supervisor.session_status(self._factory)
        except Exception as exc:
            await self._send(chat_id, f"Status unavailable: {exc}")
            return
        if summary["total"] == 0:
            await self._send(chat_id, "No active sessions.")
            return
        await self._send(
            chat_id,
            f"Sessions: {summary['total']} total, "
            f"{summary['running']} running, {summary['complete']} complete, "
            f"{summary['failed']} failed.",
        )

    async def _decide(self, chat_id: int, approval_id: str, approve: bool) -> None:
        """Grant/deny an approval (kept for backward compat: _decide is called
        directly from tests and the REST approval flow)."""
        await self._cmd_approve_run(OPERATOR, chat_id, approval_id, approve=approve)

    async def _cmd_approve_run(
        self, principal: Principal, chat_id: int, approval_id: str, *, approve: bool
    ) -> None:
        verdict = "APPROVED" if approve else "DENIED"
        try:
            if not principal.can("approval.decide"):
                await self._send(chat_id, "You cannot approve/deny.")
                return
            affected = self._gate.decide(
                approval_id,
                approve=approve,
                policy=Policy.ALLOW_ONCE,
                decided_by=str(principal.user_id or "local"),
                # Pass the real identity so the ownership gate in decide()
                # actually fires: a principal must not decide an approval owned
                # by another user (P0#3 object isolation).
                decided_by_user_id=(
                    str(principal.user_id) if principal.user_id is not None else None
                ),
            )
        except Exception as exc:
            await self._send(chat_id, f"Decision failed: {exc}")
            return
        if affected:
            await self._send(chat_id, f"Approval {approval_id} -> {verdict}")
        else:
            await self._send(
                chat_id,
                "Approval is no longer pending (expired or already decided).",
            )

    async def _cancel_task(self, principal: Principal, chat_id: int, task_id: str) -> None:
        if self._task_owned_by_another(task_id, principal):
            await self._send(chat_id, f"Task {task_id} not found or not authored by you.")
            return
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
            await self._send(chat_id, f"Task {task_id} not found or not cancelable.")

    # -- outbound delivery --------------------------------------------------

    async def _send(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        kind: str = KIND_COMMAND_RESPONSE,
        event_id: str | None = None,
    ) -> None:
        """Persist + deliver one Telegram message.

        When a DB factory is wired, the message is enqueued to the delivery
        outbox (persist-first, durable). Otherwise (tests, local without DB)
        it is sent directly via the httpx client so existing behaviour and
        tests are unchanged.
        """
        if not self.is_configured():
            return
        text = (text or "")[:4000]
        if self._outbox is not None:
            self._outbox.enqueue(
                kind=kind,
                chat_id=int(chat_id),
                text=text,
                reply_markup=reply_markup,
                event_id=event_id,
            )
            return
        await self._send_direct(chat_id, text, reply_markup)

    def _send_sync(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        kind: str = KIND_COMMAND_RESPONSE,
    ) -> None:
        """Synchronous deliver (for background threads, no async loop)."""
        if not self.is_configured():
            return
        text = (text or "")[:4000]
        if self._outbox is not None:
            self._outbox.enqueue(
                kind=kind,
                chat_id=int(chat_id),
                text=text,
                reply_markup=reply_markup,
            )
            return
        import httpx as _httpx

        try:
            resp = _httpx.Client(timeout=_HTTP_TIMEOUT).post(
                _API_BASE.format(token=self._token) + "/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    **({"reply_markup": reply_markup} if reply_markup else {}),
                },
            )
            resp.raise_for_status()
        except Exception:
            pass

    async def _send_direct(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        if not self.is_configured():
            return
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
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

    async def send_message(self, chat_id: int, text: str) -> None:
        await self._send(chat_id, text)

    async def send_notification(self, chat_id: int, text: str) -> None:
        """Fire-and-forget notification to a single authorized chat."""
        if not self._is_authorized(chat_id):
            return
        await self._send(chat_id, text, kind=KIND_NOTIFICATION)

    async def broadcast(
        self,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        """Send to every authorized chat (legacy approval request path)."""
        for chat_id in self._authorized_chat_ids():
            try:
                await self._send(chat_id, text, reply_markup=reply_markup)
            except Exception:
                continue

    async def send_approval_request(self, record: ApprovalRecord) -> None:
        """Push an approval prompt with inline approve/deny buttons.

        When a DB factory is wired, each message is enqueued to the outbox
        (persist-first, retryable). The callback ``data`` carries only the
        approval id — no action is trusted from the callback text alone
        (spec §13, §14). The server re-validates principal + scope on submit.
        """
        text = (
            "⚠️ Approval required\n"
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
        for chat_id in self._authorized_chat_ids():
            await self._send(
                chat_id,
                text,
                reply_markup=markup,
                kind=KIND_APPROVAL,
                event_id=record.approval_id,
            )
