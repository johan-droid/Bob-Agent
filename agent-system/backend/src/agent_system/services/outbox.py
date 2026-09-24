"""Durable delivery outbox (persist-first outbound delivery).

Every outbound notification (Telegram message, callback buttons during
approvals) is persisted to the ``delivery_outbox`` table BEFORE delivery is
attempted. Delivery is a best-effort retry on top of durable state — a
Telegram message is never the record of record (spec \u00a716).

Delivery loop (runs in the worker process):
- claim rows atomically (conditional UPDATE on ``next_attempt_at``)
- back off exponentially on Telegram failures
- dead-letter (``DEAD``) after ``outbox_max_attempts``
- reap claims stuck beyond ``outbox_lease_seconds``

Web handlers only enqueue; workers run ``drain_outbox``.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from agent_system.domain.events import utcnow
from agent_system.infra.models import DeliveryOutbox

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_EDIT_API = "https://api.telegram.org/bot{token}/editMessageText"
TELEGRAM_ACTION_API = "https://api.telegram.org/bot{token}/sendChatAction"

KIND_COMMAND_RESPONSE = "command_response"
KIND_APPROVAL = "approval"
KIND_NOTIFICATION = "notification"
KIND_PROGRESS_EDIT = "progress_edit"
KIND_TYPING = "typing"

_UTC = UTC


class Outbox:
    """Telegram delivery outbox client.

    ``factory`` is a zero-arg callable producing a SQLAlchemy session
    (matching ``session_scope`` / ``get_db_session`` conventions).
    """

    def __init__(self, factory: Any, settings: Any) -> None:
        self._factory = factory
        self._settings = settings

    def enqueue(
        self,
        *,
        kind: str,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        event_id: str | None = None,
        task_id: str | None = None,
        reply_to_message_id: int | None = None,
        edit_message_id: int | None = None,
    ) -> str | None:
        if chat_id is None:
            return None
        # Telegram rejects empty text with 400; never enqueue it (would retry
        # 5x per message and stall the recovery drain). Skip fail-fast.
        if not (text or "").strip():
            logger.warning("outbox.skip.empty kind=%s chat_id=%s", kind, chat_id)
            return None
        # Telegram hard limit is 4096 chars; truncate defensively.
        if len(text) > 4000:
            text = text[:4000]
        from agent_system.domain.ids import new_id
        from agent_system.infra.db import session_scope

        with session_scope(self._factory) as db:
            # Durable idempotency for event-driven enqueues: the relay
            # ``flush()`` path and the recovery ``replay()`` path can both see
            # the same event (a long drive flushes after the 10s sweep already
            # replayed it), which used to enqueue the SAME user-visible message
            # twice. ``replay()`` deduped by ``event_id`` but ``flush()`` did
            # not, so the guard belongs here — on the one write seam — making
            # every enqueue idempotent per ``event_id``.
            if event_id:
                existing = (
                    db.query(DeliveryOutbox.id).filter(DeliveryOutbox.event_id == event_id).first()
                )
                if existing is not None:
                    logger.info(
                        "outbox.duplicate.skipped event_id=%s chat_id=%s kind=%s",
                        event_id,
                        chat_id,
                        kind,
                    )
                    return str(existing[0])
            row = DeliveryOutbox(
                id=new_id("out"),
                channel="telegram",
                kind=kind,
                chat_id=str(chat_id),
                text=text,
                reply_markup_json=reply_markup,
                event_id=event_id,
                task_id=task_id,
                reply_to_message_id=reply_to_message_id,
                edit_message_id=edit_message_id,
                state="PENDING",
                attempts=0,
                next_attempt_at=utcnow(),
            )
            db.add(row)
            db.flush()
            logger.info("outbox.created outbox_id=%s chat_id=%s kind=%s", row.id, chat_id, kind)
            logger.info(
                "telegram.outbox.created outbox_id=%s chat_id=%s kind=%s", row.id, chat_id, kind
            )
            return row.id

    def claim_batch(
        self, max_rows: int = 50, lease_seconds: int | None = None, worker_id: str | None = None
    ) -> list[DeliveryOutbox]:
        from agent_system.infra.db import session_scope

        lease = lease_seconds or self._settings.outbox_lease_seconds
        now = utcnow()
        deadline = now + timedelta(seconds=lease)
        with session_scope(self._factory) as db:
            q = (
                db.query(DeliveryOutbox)
                .filter(
                    # RETRY included: a row that already failed once (state
                    # RETRY, next_attempt_at scheduled) must be re-claimed
                    # after its backoff elapses, otherwise a single Telegram
                    # API failure would permanently drop the message.
                    DeliveryOutbox.state.in_(("PENDING", "RETRY")),
                    DeliveryOutbox.next_attempt_at <= now,
                )
                .order_by(DeliveryOutbox.next_attempt_at)
                .limit(max_rows)
            )
            try:
                if getattr(db, "bind", None) is not None and db.bind.dialect.name == "postgresql":
                    q = q.with_for_update(skip_locked=True)
            except Exception:
                pass
            rows = q.all()
            claimed: list[DeliveryOutbox] = []
            for r in rows:
                affected = (
                    db.query(DeliveryOutbox)
                    .filter(
                        DeliveryOutbox.id == r.id,
                        DeliveryOutbox.state.in_(("PENDING", "RETRY")),
                        DeliveryOutbox.next_attempt_at <= now,
                    )
                    .update(
                        {
                            "state": "SENDING",
                            "next_attempt_at": deadline,
                            "claimed_at": now,
                            "claimed_by": worker_id or "worker",
                        },
                        synchronize_session=False,
                    )
                )
                if affected > 0:
                    claimed.append(r)
            if claimed:
                db.commit()
            return claimed

    def deliver_one(self, row: DeliveryOutbox, client: httpx.Client | None = None) -> bool:
        token = self._settings.telegram_bot_token
        if not token:
            return False
        # Fail-fast for legacy empty rows (pre-fix): mark delivered-skipped so
        # they stop consuming the 5-attempt retry budget and blocking drain.
        if not (row.text or "").strip():
            logger.warning("outbox.skip.empty_deliver outbox_id=%s chat_id=%s", row.id, row.chat_id)
            return self._mark_delivered(row)
        logger.info("outbox.send.start outbox_id=%s chat_id=%s", row.id, row.chat_id)
        logger.info("telegram.outbox.sent outbox_id=%s chat_id=%s", row.id, row.chat_id)
        if client is None and (token.startswith("test:") or token == "mock"):
            return self._mark_delivered(row)
        own_client = client is None
        if own_client:
            client = httpx.Client(timeout=30.0)
        # Progress edits and typing actions are best-effort: a missed stage
        # update or a missed animation must NEVER block the pipeline or burn
        # retries meant for real answers. Failed rows are marked delivered
        # (skipped) immediately.
        is_edit = row.kind == KIND_PROGRESS_EDIT and row.edit_message_id is not None
        is_typing = row.kind == KIND_TYPING
        try:
            payload: dict[str, Any]
            if is_typing:
                payload = {
                    "chat_id": int(row.chat_id),
                    "action": (row.text or "typing").strip() or "typing",
                }
                endpoint = TELEGRAM_ACTION_API.format(token=token)
            elif is_edit:
                payload = {
                    "chat_id": int(row.chat_id),
                    "message_id": int(row.edit_message_id or 0),
                    "text": row.text,
                }
                endpoint = TELEGRAM_EDIT_API.format(token=token)
            else:
                payload = {
                    "chat_id": int(row.chat_id),
                    "text": row.text,
                }
                if row.reply_markup_json:
                    payload["reply_markup"] = row.reply_markup_json
                reply_to = row.reply_to_message_id
                if reply_to is not None:
                    payload["reply_parameters"] = {
                        "message_id": int(reply_to),
                        "allow_sending_without_reply": True,
                    }
                endpoint = TELEGRAM_API.format(token=token)
            resp = client.post(  # type: ignore[union-attr]
                endpoint,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            # Capture the Telegram message id created by a sendMessage so the
            # stage that owns it can hand the editable id to later stages.
            if not is_edit and not is_typing:
                try:
                    tg_id = (resp.json() or {}).get("result", {}).get("message_id")
                    if tg_id is not None:
                        self._capture_telegram_message_id(row, int(tg_id))
                except Exception:  # pragma: no cover - capture is best-effort
                    pass
        except Exception as exc:
            if is_edit or is_typing:
                logger.warning(
                    "outbox.ephemeral.skipped outbox_id=%s chat_id=%s kind=%s error=%s",
                    row.id,
                    row.chat_id,
                    row.kind,
                    _scrub_error(exc),
                )
                return self._mark_delivered(row)
            detail = _scrub_error(exc)
            try:
                import httpx as _httpx2

                if isinstance(exc, _httpx2.HTTPStatusError) and exc.response is not None:
                    body = exc.response.text[:300]
                    if body:
                        detail = f"{detail} body={body}"
            except Exception:
                pass
            logger.warning(
                "outbox.send.failure outbox_id=%s chat_id=%s error=%s",
                row.id,
                row.chat_id,
                detail,
            )
            logger.warning(
                "telegram.delivery.failed outbox_id=%s chat_id=%s error=%s",
                row.id,
                row.chat_id,
                detail,
            )
            self._advance_failure(row, exc)
        else:
            return self._mark_delivered(row)
        finally:
            if own_client and client is not None:
                client.close()
        return False

    def _capture_telegram_message_id(self, row: DeliveryOutbox, tg_message_id: int) -> None:
        """Persist the Telegram message id created by a delivered sendMessage."""
        try:
            from agent_system.infra.db import session_scope

            with session_scope(self._factory) as db:
                db.query(DeliveryOutbox).filter(DeliveryOutbox.id == row.id).update(
                    {"telegram_message_id": tg_message_id},
                    synchronize_session=False,
                )
                db.commit()
        except Exception:  # pragma: no cover - capture is best-effort
            logger.warning("outbox.capture.message_id.failed outbox_id=%s", row.id)

    def telegram_message_id_for(self, outbox_id: str) -> int | None:
        """Read back the captured Telegram message id for an outbox row."""
        try:
            from agent_system.infra.db import session_scope

            with session_scope(self._factory) as db:
                row = db.get(DeliveryOutbox, outbox_id)
                if row is not None:
                    return row.telegram_message_id
        except Exception:
            pass
        return None

    def _mark_delivered(self, row: DeliveryOutbox) -> bool:
        from agent_system.infra.db import session_scope

        with session_scope(self._factory) as db:
            affected = (
                db.query(DeliveryOutbox)
                .filter(
                    DeliveryOutbox.id == row.id,
                    # SENDING is the claimed state; PENDING/RETRY kept for
                    # backward-compat with rows claimed before upgrade.
                    DeliveryOutbox.state.in_(("PENDING", "RETRY", "SENDING")),
                )
                .update(
                    {
                        "state": "DELIVERED",
                        "delivered_at": utcnow(),
                        "claimed_at": None,
                        # next_attempt_at kept
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            if affected > 0:
                logger.info("outbox.send.success outbox_id=%s chat_id=%s", row.id, row.chat_id)
                logger.info(
                    "telegram.response.delivered outbox_id=%s chat_id=%s", row.id, row.chat_id
                )
            return affected > 0

    def _advance_failure(self, row: DeliveryOutbox, exc: Exception) -> None:
        from agent_system.infra.db import session_scope

        row.attempts += 1
        next_delay = min(
            self._settings.outbox_backoff_base_seconds * (2 ** (row.attempts - 1)),
            60.0,
        )
        if row.attempts >= self._settings.outbox_max_attempts:
            state = "DEAD"
        else:
            state = "RETRY"
        with session_scope(self._factory) as db:
            db.query(DeliveryOutbox).filter(DeliveryOutbox.id == row.id).update(
                {
                    "attempts": row.attempts,
                    "last_error": _scrub_error(exc),
                    "state": state,
                    "next_attempt_at": utcnow() + timedelta(seconds=next_delay),
                },
                synchronize_session=False,
            )
            db.commit()
        logger.warning(
            "outbox row %s delivery failed (%d/%d): %s",
            row.id,
            row.attempts,
            self._settings.outbox_max_attempts,
            _scrub_error(exc),
        )

    def drain(self, max_rows: int = 50, client: httpx.Client | None = None) -> int:
        rows = self.claim_batch(max_rows=max_rows)
        for row in rows:
            self.deliver_one(row, client=client)
        return len(rows)

    def reap_stuck(self, now: datetime | None = None) -> int:
        from agent_system.infra.db import session_scope

        cutoff = (now or utcnow()) - timedelta(seconds=self._settings.outbox_lease_seconds)
        with session_scope(self._factory) as db:
            updated = (
                db.query(DeliveryOutbox)
                .filter(
                    DeliveryOutbox.state.in_(("PENDING", "SENDING", "RETRY")),
                    DeliveryOutbox.claimed_at <= cutoff,
                )
                .update(
                    {
                        "claimed_at": None,
                        "next_attempt_at": utcnow(),
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            return updated


def _scrub_error(exc: Exception) -> str:
    text = str(exc)
    text = re.sub(r"token=[\w:.:-]+", "token=[REDACTED]", text)
    text = re.sub(r"Bearer\s+\S+", "Bearer [REDACTED]", text)
    return text[:200]
