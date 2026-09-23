"""Telegram Gateway Pipeline: durable relay between Telegram and Bob core.

Pipeline (persist -> ack -> task -> execute -> persist result -> outbound ->
deliver -> mark delivered):

    Telegram Update
      -> ``telegram_updates`` ledger (persist + idempotency gate)
      -> :class:`GatewayExecutor` claims unprocessed rows
      -> Session + master task via the normal Supervisor (durable)
      -> "Task created: #id" ack (durable outbox)
      -> ``drive_session`` (Agent Runtime: LLM router / skills / MCP / swarm)
      -> ``task.completed`` / ``task.failed`` / ``approval.requested`` events
         queued at emit-time and flushed by :class:`GatewayRelay`
      -> ``DeliveryOutbox`` (persist result) -> Telegram API -> delivered

Crash safety: every stage is durable. ``recover()`` re-drives sessions of
unprocessed updates, replays relay-able events from the append-only event
store, and the outbox re-delivers unsent messages — so "agent finished but
Telegram never received it" self-heals after a dyno restart.

Locking note: the EventBus fans subscribers out INSIDE the emitter's
transaction. On SQLite a second-connection write at that point would wait on
the emitter's write lock (callbacks block the commit -> busy-timeout loss).
The relay therefore only queues in memory at emit-time and persists to the
outbox in ``flush()`` — called by the executor after the drive loop returns
and by ``recover()``. The event store remains the source of truth either way.

Telegram is only an interface: no business logic lives here — the same Bob
engine serves Telegram / Web / CLI / API.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any

from agent_system.config import Settings
from agent_system.domain.events import Event
from agent_system.domain.ids import new_id
from agent_system.infra.db import session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import (
    Approval,
    DeliveryOutbox,
    EventRow,
    Session,
    Task,
    TelegramGatewayMessage,
    TelegramUpdate,
)
from agent_system.services.identity import Role

_logger = logging.getLogger(__name__)

KIND_TASK_ACK = "command_response"
KIND_RESULT = "notification"

RELAYED_EVENT_TYPES = ("task.completed", "task.failed", "approval.requested")


def record_session_chat(factory: Any, session_id: str, chat_id: int) -> None:
    """Durable chat<->session mapping (Telegram Gateway E2E, additive).

    Called by the Telegram interface when a session is created from a chat.
    Writes a ``telegram_gateway_messages`` row (no update id — this is the
    session-level binding) so the result relay can find the chat even after a
    restart, independent of the per-update ingest ledger. Idempotent: a
    session is bound at most once per chat.
    """
    from agent_system.domain.events import utcnow

    with session_scope(factory) as db:
        existing = (
            db.query(TelegramGatewayMessage.id)
            .filter(
                TelegramGatewayMessage.session_id == session_id,
                TelegramGatewayMessage.chat_id == str(chat_id),
            )
            .first()
        )
        if existing is not None:
            return
        db.add(
            TelegramGatewayMessage(
                id=new_id("tgm"),
                telegram_update_id=None,
                chat_id=str(chat_id),
                session_id=session_id,
                received_at=utcnow(),
                processing_status="DISPATCHED",
            )
        )


class GatewayRelay:
    """Event -> Telegram fan-out (acks out of scope; results + approvals).

    Emit-time: queue only (no DB — see module locking note). Flush-time:
    persist one outbound message per event, resolved chat-first: Task ->
    Session.owner -> latest ingest row for that owner. Delivery itself goes
    through the outbox so a Telegram API failure retries without losing the
    message.
    """

    def __init__(self, factory: Any, outbox: Any, bus: EventBus) -> None:
        self._factory = factory
        self._outbox = outbox
        self._bus = bus
        self._active = False
        self._pending: deque[Event] = deque()

    def start(self) -> None:
        if self._active:
            return
        for event_type in RELAYED_EVENT_TYPES:
            self._bus.subscribe(event_type, self._on_event)
        self._active = True

    def stop(self) -> None:
        self._active = False

    # -- emit-time: queue only ------------------------------------------------

    def _on_event(self, event: Event) -> None:
        try:
            if event.type == "approval.requested" or event.type in (
                "task.completed",
                "task.failed",
            ):
                self._pending.append(event)
        except Exception:  # pragma: no cover - queueing cannot fail
            _logger.exception("gateway relay queue failed for %s", event.type)

    # -- flush-time: durable enqueue -------------------------------------------

    def flush(self) -> int:
        """Persist all queued events to the outbox. Returns rows enqueued."""
        enqueued = 0
        while self._pending:
            event = self._pending.popleft()
            try:
                if self._relay(event):
                    enqueued += 1
            except Exception:
                _logger.exception("gateway relay flush failed for %s", event.type)
        return enqueued

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def _relay(self, event: Event) -> bool:
        if event.type == "approval.requested":
            return self._relay_approval(event)
        return self._relay_result(event)

    # -- chat resolution -------------------------------------------------------

    def _chat_for_owner(self, owner: str | None) -> int | None:
        """Bob user id -> provisioned Telegram chat (primary resolution).

        ``owner_user_id`` is a Bob user id; the TelegramAccount link table is
        the authoritative owner -> chat mapping (same resolution the legacy
        broadcast path uses). Unknown/inactive/blocked owners get no chat.
        """
        if owner is None:
            return None
        from agent_system.infra.models import TelegramAccount, User

        with session_scope(self._factory) as db:
            row = (
                db.query(TelegramAccount)
                .join(User, TelegramAccount.user_id == User.id)
                .filter(
                    TelegramAccount.user_id == str(owner),
                    TelegramAccount.role != Role.BLOCKED.value,
                    User.is_active.is_(True),
                    TelegramAccount.chat_id.isnot(None),
                )
                .first()
            )
            if row is None or row.chat_id is None:
                return None
            try:
                return int(row.chat_id)
            except (TypeError, ValueError):
                return None

    def _chat_for_task(self, task_id: str) -> int | None:
        """Resolve the Telegram chat that owns a task.

        Gateway-state first (``telegram_gateway_messages``), then the
        owner -> latest-ingest fallback so pre-gateway data keeps working.
        """
        with session_scope(self._factory) as db:
            row = (
                db.query(TelegramGatewayMessage)
                .filter(TelegramGatewayMessage.task_id == task_id)
                .order_by(TelegramGatewayMessage.received_at.desc())
                .first()
            )
            if row is not None and row.chat_id is not None:
                try:
                    return int(row.chat_id)
                except (TypeError, ValueError):
                    pass
            task = db.get(Task, task_id)
            if task is None:
                return None
            session = db.get(Session, task.session_id)
            owner = session.owner_user_id if session is not None else None
        return self._chat_for_owner(owner)

    def _chat_for_approval(self, approval_id: str) -> int | None:
        with session_scope(self._factory) as db:
            approval = db.get(Approval, approval_id)
            if approval is None:
                return None
            owner = approval.owner_user_id
            task_id = approval.task_id
        if owner is not None:
            chat = self._chat_for_owner(owner)
            if chat is not None:
                return chat
        if not task_id:
            return None
        return self._chat_for_task(task_id)

    # -- relay targets ---------------------------------------------------------

    def _relay_result(self, event: Event) -> bool:
        from agent_system.services.telegram_presenter import (
            format_model_footer,
            sanitize_telegram_message,
            save_chat_message,
        )

        task_id = event.task_id
        if not task_id:
            return False
        chat_id = self._chat_for_task(task_id)
        if chat_id is None:
            return False
        if event.type == "task.completed":
            output = str(event.payload.get("output") or "Done.")
            clean_output = sanitize_telegram_message(output)
            provider = str(event.payload.get("provider") or event.payload.get("actor") or "groq")
            model_id = str(event.payload.get("model") or event.payload.get("model_id") or "default")
            lat_ms = event.payload.get("latency_ms")
            latency_s = float(lat_ms) / 1000.0 if lat_ms is not None else None
            footer = format_model_footer(provider, model_id, latency_s)
            if footer not in clean_output:
                text = f"{clean_output}\n\n{footer}"
            else:
                text = clean_output
            save_chat_message(self._factory, chat_id, "assistant", clean_output)
        else:
            text = "Sorry, I ran into an issue while fulfilling your request. Please try again."
        self._outbox.enqueue(
            kind=KIND_RESULT,
            chat_id=int(chat_id),
            text=text[:4000],
            task_id=task_id,
            event_id=event.event_id,
        )
        return True

    def _relay_approval(self, event: Event) -> bool:
        approval_id = str(event.payload.get("approval_id") or event.payload.get("id") or "")
        if not approval_id:
            return False
        chat_id = self._chat_for_approval(approval_id)
        if chat_id is None:
            return False
        action = str(
            event.payload.get("requested_action") or event.payload.get("action") or "action"
        )
        self._outbox.enqueue(
            kind="approval",
            chat_id=int(chat_id),
            text=(f"⚠️ Approval required\n\nWorker wants to:\n`{action[:600]}`")[:4000],
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "Approve", "callback_data": f"approve:{approval_id}"},
                        {"text": "Deny", "callback_data": f"deny:{approval_id}"},
                    ]
                ]
            },
            task_id=event.task_id,
            event_id=event.event_id,
        )
        return True

    # -- recovery: replay from the append-only event store ---------------------

    def replay(self, within_minutes: int = 1440) -> int:
        """Re-enqueue relay-able events missing an outbox row (restart path).

        The event store is the source of truth: if the process died between a
        task completing and its result reaching the outbox, ``replay()`` finds
        the unrelayed events and enqueues them. Deduped by ``event_id``.
        """
        from datetime import timedelta

        from agent_system.domain.events import utcnow

        cutoff = utcnow() - timedelta(minutes=within_minutes)
        enqueued = 0
        with session_scope(self._factory) as db:
            rows = (
                db.query(EventRow)
                .filter(EventRow.type.in_(RELAYED_EVENT_TYPES), EventRow.timestamp >= cutoff)
                .order_by(EventRow.sequence)
                .all()
            )
            candidates = [
                Event(
                    event_id=r.event_id,
                    schema_version=r.schema_version,
                    session_id=r.session_id,
                    task_id=r.task_id,
                    agent_run_id=r.agent_run_id,
                    timestamp=r.timestamp,
                    type=r.type,
                    actor=r.actor,
                    payload=r.payload or {},
                )
                for r in rows
            ]
        for event in candidates:
            with session_scope(self._factory) as db:
                # Dedup: skip events that already produced an outbox row.
                has_outbox = (
                    db.query(DeliveryOutbox.id)
                    .filter(DeliveryOutbox.event_id == event.event_id)
                    .first()
                    is not None
                )
            if has_outbox:
                continue
            try:
                if self._relay(event):
                    enqueued += 1
            except Exception:
                _logger.exception("gateway replay failed for %s", event.event_id)
        return enqueued


class GatewayExecutor:
    """Durable update -> task executor (works without a bot token).

    Claims unprocessed rows from the ``telegram_updates`` ledger, creates
    exactly one durable Session + master task per goal, acknowledges via the
    outbox, drives the session through the normal Bob runtime, flushes relayed
    results, and marks the update processed only after execution. Re-running
    is safe: processed rows are skipped, so a retried update never duplicates
    a task.
    """

    def __init__(self, settings: Settings, factory: Any, bus: EventBus) -> None:
        self._settings = settings
        self._factory = factory
        self._bus = bus
        self._in_flight: set[int] = set()
        self._in_flight_lock = threading.Lock()
        from agent_system.services.outbox import Outbox

        self._outbox = Outbox(factory, settings)
        self._relay = GatewayRelay(factory, self._outbox, bus)

    @property
    def relay(self) -> GatewayRelay:
        return self._relay

    @property
    def outbox(self) -> Any:
        return self._outbox

    def start(self) -> None:
        self._relay.start()

    def stop(self) -> None:
        self._relay.stop()

    # -- chat resolution (executor side) ---------------------------------------

    def _owner_and_chat(
        self, update_id: int
    ) -> tuple[str | None, int | None, str | None, int | None, str, bool]:
        """Owner account id + chat id + user id + message id + goal text + already_processed."""
        with session_scope(self._factory) as db:
            row = db.get(TelegramUpdate, update_id)
            if row is None:
                return None, None, None, None, "", True
            if row.processed_at is not None:
                return None, None, None, None, "", True
            payload = row.payload_json or {}
            message = payload.get("message") or {}
            text = str(message.get("text") or "").strip()
            chat_raw = row.chat_id or (message.get("chat") or {}).get("id")
            account_id = row.account_id
            from_user = message.get("from") or {}
            if account_id is None:
                account_id = str(from_user.get("id")) if from_user.get("id") else None
            user_id = str(from_user.get("id")) if from_user.get("id") else None
            message_id = message.get("message_id")
            try:
                chat_id = int(chat_raw) if chat_raw is not None else None
            except (TypeError, ValueError):
                chat_id = None
            try:
                message_id = int(message_id) if message_id is not None else None
            except (TypeError, ValueError):
                message_id = None
            return account_id, chat_id, user_id, message_id, text, False

    # -- pipeline stages --------------------------------------------------------

    def process_pending(self, limit: int = 20, background: bool = False) -> int:
        """Claim and execute unprocessed updates. Returns count processed."""
        _logger.info("gateway.pending.start limit=%s", limit)
        from sqlalchemy import select

        with session_scope(self._factory) as db:
            active_statuses = ("IN_PROGRESS", "DISPATCHED", "COMPLETED")
            claimed_stmt = select(TelegramGatewayMessage.telegram_update_id).where(
                TelegramGatewayMessage.telegram_update_id.isnot(None),
                TelegramGatewayMessage.processing_status.in_(active_statuses),
            )
            pending = (
                db.query(TelegramUpdate.update_id)
                .filter(
                    TelegramUpdate.processed_at.is_(None),
                    ~TelegramUpdate.update_id.in_(claimed_stmt),
                )
                .order_by(TelegramUpdate.update_id)
                .limit(limit)
                .all()
            )
        processed = 0
        for (update_id,) in pending:
            uid = int(update_id)
            try:
                if self._process_one(uid, background=background):
                    processed += 1
            except Exception as exc:
                _logger.exception("gateway executor failed for update %s: %s", update_id, exc)
        _logger.info("gateway.pending.complete processed=%s", processed)
        return processed

    def _resolve_owner(self, account_id: str | None, chat_id: int | None = None) -> str | None:
        """Authenticate the ledger account: TelegramAccount -> active Bob user.

        The telegram user id in an update payload is identity, never
        authorization — it is re-verified server-side at execution time.
        Unknown, inactive or blocked accounts are never executed (deny by
        default, no oracle to probing senders).

        If identity mode is `telegram` and `account_id` is in the allowed user IDs list
        but not yet provisioned, auto-provision it.
        """
        from agent_system.services.identity import IdentityMode, IdentityService

        identity_mode = getattr(self._settings, "agent_identity_mode", "local")

        # 1. DB lookup if account_id is present
        if account_id:
            from agent_system.infra.models import TelegramAccount, User

            with session_scope(self._factory) as db:
                row = (
                    db.query(
                        TelegramAccount.user_id,
                        TelegramAccount.role.label("acct_role"),
                        User.is_active,
                        User.role.label("user_role"),
                    )
                    .join(User, TelegramAccount.user_id == User.id)
                    .filter(TelegramAccount.telegram_user_id == str(account_id))
                    .one_or_none()
                )
                if row is not None:
                    if (
                        not row.is_active
                        or str(row.user_role) == Role.BLOCKED.value
                        or str(row.acct_role) == Role.BLOCKED.value
                    ):
                        return None
                    return str(row.user_id)

            # Auto-provision if in identity mode and listed in allowed user IDs
            if identity_mode == IdentityMode.TELEGRAM.value:
                identity_svc = IdentityService(self._factory, self._settings)
                if str(account_id) in identity_svc.allowed_user_ids():
                    try:
                        principal = identity_svc.provision(
                            str(account_id),
                            display_name=f"telegram:{account_id}",
                            chat_id=chat_id,
                        )
                        return principal.user_id
                    except Exception as exc:
                        _logger.warning(
                            "Gateway auto-provision failed for account %s: %s", account_id, exc
                        )
                        return None

        # 2. Local mode fallback (chat-id allowlist)
        if identity_mode == "local" or identity_mode == IdentityMode.LOCAL.value:
            if chat_id is not None and chat_id in self._settings.allowed_chat_ids:
                return "local"

        return None

    # -- gateway state (telegram_gateway_messages) -------------------------------

    def _gateway_begin(
        self, update_id: int, chat_id: int, user_id: str | None, message_id: int | None
    ) -> None:
        """PERSIST stage: first-class gateway state for the inbound update."""
        from agent_system.domain.events import utcnow

        with session_scope(self._factory) as db:
            row = (
                db.query(TelegramGatewayMessage)
                .filter(TelegramGatewayMessage.telegram_update_id == update_id)
                .one_or_none()
            )
            if row is None:
                db.add(
                    TelegramGatewayMessage(
                        id=new_id("tgm"),
                        telegram_update_id=update_id,
                        chat_id=str(chat_id),
                        user_id=user_id,
                        message_id=message_id,
                        received_at=utcnow(),
                        processing_status="RECEIVED",
                    )
                )

    def _gateway_bound(self, update_id: int, session_id: str, task_id: str | None) -> None:
        """UPDATE stage: the session/task this update created (idempotency link)."""
        with session_scope(self._factory) as db:
            row = (
                db.query(TelegramGatewayMessage)
                .filter(TelegramGatewayMessage.telegram_update_id == update_id)
                .one_or_none()
            )
            if row is not None:
                row.session_id = session_id
                row.task_id = task_id
                row.processing_status = "DISPATCHED"

    def _gateway_done(self, update_id: int) -> None:
        """COMPLETED stage: execution finished (success or not) for this update."""
        with session_scope(self._factory) as db:
            row = (
                db.query(TelegramGatewayMessage)
                .filter(TelegramGatewayMessage.telegram_update_id == update_id)
                .one_or_none()
            )
            if row is not None and row.processing_status != "COMPLETED":
                row.processing_status = "COMPLETED"

    def _drive_and_finish(self, update_id: int, session_id: str, chat_id: int) -> None:
        from agent_system.services.cloud import drive_session
        from agent_system.services.telegram_presenter import TelegramProgressPresenter

        presenter = TelegramProgressPresenter(
            self._factory, self._outbox, chat_id, session_id, self._bus
        )
        presenter.start()
        from agent_system.services.telegram import send_chat_action_sync as _typing

        _typing(getattr(self._settings, "telegram_bot_token", None), chat_id)

        try:
            _logger.info("telegram.agent.started session_id=%s", session_id)
            # The executor's own settings govern the whole drive
            # (single-config execution — never a divergent ambient read).
            drive_session(self._factory, self._bus, session_id, settings=self._settings)
            _logger.info("telegram.task.completed session_id=%s", session_id)
        except Exception as exc:
            _logger.exception(
                "telegram.agent.failed update_id=%s chat_id=%s session_id=%s error=%s",
                update_id,
                chat_id,
                session_id,
                exc,
            )
            self._outbox.enqueue(
                kind=KIND_RESULT,
                chat_id=chat_id,
                text="Sorry, I ran into an issue while fulfilling your request. Please try again.",
            )
        finally:
            try:
                presenter.stop()
                self._relay.flush()
                self._gateway_done(update_id)
                self._mark_processed(update_id)
            finally:
                with self._in_flight_lock:
                    self._in_flight.discard(update_id)

    def _process_one(self, update_id: int, background: bool = False) -> bool:
        with self._in_flight_lock:
            if update_id in self._in_flight:
                _logger.info("gateway.update.in_flight_skip update_id=%s", update_id)
                return False
            self._in_flight.add(update_id)
        account_id, chat_id, user_id, message_id, text, already_processed = self._owner_and_chat(
            update_id
        )
        if already_processed:
            with self._in_flight_lock:
                self._in_flight.discard(update_id)
            return False
        _logger.info("gateway.update.claimed update_id=%s chat_id=%s", update_id, chat_id)
        owner = self._resolve_owner(account_id, chat_id=chat_id)
        if owner is None:
            _logger.info(
                "gateway.identity.denied update_id=%s chat_id=%s account_id=%s",
                update_id,
                chat_id,
                account_id,
            )
            self._mark_processed(update_id)
            with self._in_flight_lock:
                self._in_flight.discard(update_id)
            return True
        if not text or chat_id is None:
            self._mark_processed(update_id)
            with self._in_flight_lock:
                self._in_flight.discard(update_id)
            return True
        _logger.info(
            "gateway.identity.resolved update_id=%s chat_id=%s owner=%s",
            update_id,
            chat_id,
            owner,
        )

        from agent_system.domain.events import utcnow
        from agent_system.domain.tasks import TaskState
        from agent_system.services.classifier import classify_request_type
        from agent_system.services.cloud import ensure_session_tasks
        from agent_system.services.orchestrator import Supervisor
        from agent_system.services.providers import build_model_router

        req_type = classify_request_type(text)
        _logger.info("gateway.request.classified update_id=%s req_type=%s", update_id, req_type)

        # Atomic DB-level claim check on TelegramGatewayMessage
        with session_scope(self._factory) as db:
            tgm = (
                db.query(TelegramGatewayMessage)
                .filter(TelegramGatewayMessage.telegram_update_id == update_id)
                .one_or_none()
            )
            claimed_states = ("IN_PROGRESS", "DISPATCHED", "COMPLETED")
            if tgm is not None and tgm.processing_status in claimed_states:
                if tgm.received_at and (utcnow() - tgm.received_at).total_seconds() < 600:
                    _logger.info("gateway.update.db_claimed_skip update_id=%s", update_id)
                    with self._in_flight_lock:
                        self._in_flight.discard(update_id)
                    return False
            if tgm is None:
                db.add(
                    TelegramGatewayMessage(
                        id=new_id("tgm"),
                        telegram_update_id=update_id,
                        chat_id=str(chat_id) if chat_id else None,
                        user_id=user_id,
                        message_id=message_id,
                        received_at=utcnow(),
                        processing_status="IN_PROGRESS",
                    )
                )
            else:
                tgm.processing_status = "IN_PROGRESS"
            db.commit()

        # -------------------------------------------------------------------
        # Lightweight CHAT Path
        # -------------------------------------------------------------------
        if req_type == "CHAT":
            from agent_system.services.memory_hooks import recall_recent
            from agent_system.services.soul import load_soul
            from agent_system.services.telegram_presenter import (
                format_model_footer,
                load_chat_history,
                sanitize_telegram_message,
                save_chat_message,
            )

            from agent_system.services.telegram import send_chat_action_sync

            send_chat_action_sync(
                getattr(self._settings, "telegram_bot_token", None), chat_id
            )
            save_chat_message(self._factory, chat_id, "user", text)
            history = load_chat_history(self._factory, chat_id, limit=10)

            prompt_lines = [
                "Respond conversationally, concisely, and helpfully as Bob "
                "according to your identity."
            ]
            if history:
                prompt_lines.append("\nRecent Conversation:")
                for msg in history[:-1]:
                    role_lbl = "User" if msg["role"] == "user" else "Assistant"
                    prompt_lines.append(f"{role_lbl}: {msg['content']}")

            try:
                notes = recall_recent(self._settings, text, limit=3, factory=self._factory)
                if notes:
                    rendered = "\n".join(f"- {n['title']}: {n['snippet'][:200]}" for n in notes)
                    prompt_lines.append(f"\nRelevant Memories:\n{rendered}")
            except Exception:
                pass

            connected_integrations: list[str] = []
            if self._factory:
                try:
                    from agent_system.services.capabilities import CapabilityRegistry
                    from agent_system.services.credentials import CredentialStore

                    vault = CredentialStore(self._factory)
                    registry = CapabilityRegistry(vault)
                    caps = registry.get_user_capabilities(owner or "local")
                    connected_integrations = [c.provider for c in caps if c.status == "healthy"]
                except Exception:
                    pass

            if "google" in connected_integrations or "gmail" in connected_integrations:
                prompt_lines.append("System Context: Google/Gmail integration IS connected.")
            else:
                prompt_lines.append(
                    "System Context: Google/Gmail integration is NOT connected. "
                    "If asked about Gmail, clearly state it is not connected (/setup google)."
                )

            prompt_lines.append(f"\nUser: {text}")
            prompt = "\n".join(prompt_lines)

            try:
                _, soul_text = load_soul(getattr(self._settings, "soul_path", "") or None)
                router = build_model_router(self._bus, self._settings, soul_text=soul_text or None)
                # 60s per attempt; the router fails over across providers
                # (llm_max_fallback_attempts) instead of hanging on one.
                inv = router.invoke(
                    self._factory,
                    router.default_model,
                    prompt,
                    agent_type="chat",
                    timeout=60,
                    fallback=True,
                )
                if inv.ok and inv.output:
                    answer = inv.output
                    provider = getattr(inv, "provider", None) or "groq"
                    model_id = getattr(inv, "model_id", None) or router.default_model
                    lat_ms = getattr(inv, "latency_ms", None)
                    latency_s = float(lat_ms) / 1000.0 if lat_ms is not None else None
                    clean_answer = sanitize_telegram_message(answer)
                    footer = format_model_footer(provider, model_id, latency_s)
                    final_text = (
                        f"{clean_answer}\n\n{footer}"
                        if footer not in clean_answer
                        else clean_answer
                    )
                else:
                    err_detail = getattr(inv, "error", None) or "Model returned empty response."
                    _logger.warning("gateway.chat.failed error=%s", err_detail)
                    clean_answer = (
                        "I'm sorry, I encountered an issue reaching the model service "
                        "to respond to your message. Please try again shortly."
                    )
                    final_text = clean_answer
            except Exception as exc:
                _logger.warning("gateway.chat.fallback error=%s", exc)
                clean_answer = (
                    "I'm sorry, I encountered an issue reaching the model service "
                    "to respond to your message. Please try again shortly."
                )
                final_text = clean_answer

            save_chat_message(self._factory, chat_id, "assistant", clean_answer)

            self._outbox.enqueue(kind=KIND_RESULT, chat_id=chat_id, text=final_text[:4000])
            self._outbox.drain()
            self._gateway_done(update_id)
            self._mark_processed(update_id)
            with self._in_flight_lock:
                self._in_flight.discard(update_id)
            return True

        # -------------------------------------------------------------------
        # Durable AGENT Task Path (TOOL_TASK, CODING_TASK, RESEARCH_TASK, etc.)
        # -------------------------------------------------------------------
        from agent_system.services.telegram_presenter import build_task_ack, save_chat_message

        save_chat_message(self._factory, chat_id, "user", text)
        self._outbox.enqueue(
            kind="command_response",
            chat_id=chat_id,
            text=build_task_ack(text, req_type),
        )
        session_id = None
        with session_scope(self._factory) as db:
            row = (
                db.query(TelegramGatewayMessage)
                .filter(TelegramGatewayMessage.telegram_update_id == update_id)
                .one_or_none()
            )
            if row is not None and row.session_id is not None:
                session_id = row.session_id
            elif chat_id is not None and message_id is not None:
                existing_msg = (
                    db.query(TelegramGatewayMessage)
                    .filter(
                        TelegramGatewayMessage.chat_id == str(chat_id),
                        TelegramGatewayMessage.message_id == message_id,
                        TelegramGatewayMessage.session_id.isnot(None),
                    )
                    .first()
                )
                if existing_msg is not None and existing_msg.session_id is not None:
                    session_id = existing_msg.session_id
                    if row is not None:
                        row.session_id = session_id
                        row.processing_status = "DISPATCHED"

        if session_id is not None:
            with session_scope(self._factory) as db:
                tasks = db.query(Task).filter(Task.session_id == session_id).all()
                term_states = (
                    TaskState.SUCCEEDED.value,
                    TaskState.FAILED.value,
                    TaskState.CANCELLED.value,
                )
                if tasks and all(t.state in term_states for t in tasks):
                    _logger.info(
                        "gateway.session.already_terminal update_id=%s session_id=%s",
                        update_id,
                        session_id,
                    )
                    self._gateway_done(update_id)
                    self._mark_processed(update_id)
                    with self._in_flight_lock:
                        self._in_flight.discard(update_id)
                    return True

        if session_id is None:
            supervisor = Supervisor(self._bus)
            session_id = supervisor.create_session(self._factory, text, owner_user_id=owner)
            _logger.info(
                "telegram.session.created update_id=%s session_id=%s", update_id, session_id
            )
            task_ids = ensure_session_tasks(self._factory, self._bus, session_id)
            master_task_id = task_ids[0] if task_ids else None
            _logger.info(
                "gateway.task.created update_id=%s session_id=%s task_id=%s",
                update_id,
                session_id,
                master_task_id,
            )
            self._gateway_bound(update_id, session_id, master_task_id)

        if background:
            import threading

            thread = threading.Thread(
                target=self._drive_and_finish,
                args=(update_id, session_id, chat_id),
                name=f"gateway-drive-{update_id}",
                daemon=True,
            )
            thread.start()
        else:
            self._drive_and_finish(update_id, session_id, chat_id)
        return True

    def _mark_processed(self, update_id: int) -> None:
        from agent_system.domain.events import utcnow

        with session_scope(self._factory) as db:
            row = db.get(TelegramUpdate, update_id)
            if row is not None and row.processed_at is None:
                row.processed_at = utcnow()

    # -- recovery ----------------------------------------------------------------

    def recover(self, background: bool = False) -> dict[str, int]:
        """Restart recovery: resume undelivered work.

        - relay replay: events that completed but never reached the outbox
        - outbox drain: undelivered messages retried at the Telegram API
        - ledger: re-drive updates that never reached processed (crash window)
        """
        replayed = self._relay.replay()
        self._relay.flush()
        redelivered = self._outbox.drain()
        self._outbox.reap_stuck()
        redriven = self.process_pending(background=background)
        return {
            "events_replayed": replayed,
            "outbox_redelivered": redelivered,
            "updates_redriven": redriven,
        }
