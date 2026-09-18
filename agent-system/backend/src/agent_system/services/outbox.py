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

KIND_COMMAND_RESPONSE = "command_response"
KIND_ACTIVITY = "activity"
KIND_APPROVAL = "approval"
KIND_NOTIFICATION = "notification"

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
    ) -> str | None:
        if chat_id is None:
            return None
        from agent_system.domain.ids import new_id
        from agent_system.infra.db import session_scope

        with session_scope(self._factory) as db:
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
                state="PENDING",
                attempts=0,
                next_attempt_at=utcnow(),
            )
            db.add(row)
            db.flush()
            return row.id

    def claim_batch(
        self, max_rows: int = 50, lease_seconds: int | None = None, worker_id: str | None = None
    ) -> list[DeliveryOutbox]:
        from agent_system.infra.db import session_scope

        lease = lease_seconds or self._settings.outbox_lease_seconds
        now = utcnow()
        deadline = now + timedelta(seconds=lease)
        with session_scope(self._factory) as db:
            rows = (
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
                .all()
            )
            claimed: list[DeliveryOutbox] = []
            for r in rows:
                affected = (
                    db.query(DeliveryOutbox)
                    .filter(
                        DeliveryOutbox.id == r.id,
                        DeliveryOutbox.next_attempt_at == r.next_attempt_at,
                    )
                    .update(
                        {
                            "next_attempt_at": deadline,
                            "claimed_at": now,
                            "claimed_by": "worker",
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
        if client is None and (token.startswith("test:") or token == "mock"):
            return self._mark_delivered(row)
        own_client = client is None
        if own_client:
            client = httpx.Client(timeout=30.0)
        try:
            payload: dict[str, Any] = {
                "chat_id": int(row.chat_id),
                "text": row.text,
                "reply_markup": row.reply_markup_json or {},
            }
            reply_to = row.reply_to_message_id
            if reply_to is not None:
                payload["reply_parameters"] = {
                    "message_id": int(reply_to),
                    "allow_sending_without_reply": True,
                }
            resp = client.post(  # type: ignore[union-attr]
                TELEGRAM_API.format(token=token),
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
        except Exception as exc:
            self._advance_failure(row, exc)
        else:
            return self._mark_delivered(row)
        finally:
            if own_client and client is not None:
                client.close()
        return False

    def _mark_delivered(self, row: DeliveryOutbox) -> bool:
        from agent_system.infra.db import session_scope

        with session_scope(self._factory) as db:
            affected = (
                db.query(DeliveryOutbox)
                .filter(
                    DeliveryOutbox.id == row.id,
                    # RETRY included: a row reclaimed after a failure and
                    # delivered on retry must still be markable as delivered.
                    DeliveryOutbox.state.in_(("PENDING", "RETRY")),
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
                    DeliveryOutbox.state == "PENDING",
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
