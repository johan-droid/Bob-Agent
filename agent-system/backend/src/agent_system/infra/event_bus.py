"""EventBus — the single canonical event pipeline (v3.1 §6).

Persists events to SQLite with a globally monotonic sequence number, fans out
to in-memory subscribers (WS/SSE bridges attach here in Phase 3/9). Redaction
of secrets happens at emit-time so sensitive values never enter the store.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent_system.domain.events import (
    Event,
    EventSensitivity,
    EventVisibility,
    validate_event_type,
)
from agent_system.infra.models import EventRow

Subscriber = Callable[[Event], None]

#: Bounded retries for sequence races: two buses (threads, processes) can read
#: the same max sequence and collide on the UNIQUE constraint. Each retry
#: reseeds from the new max, so concurrent emitters converge instead of one
#: of them failing the whole transaction.
_EMIT_MAX_ATTEMPTS = 10
#: Backoff before a sequence retry (seconds, indexed by attempt): the pause
#: lets the winning writer commit so the reseed observes it. Capped small —
#: this path only runs on an actual collision.
_EMIT_RETRY_DELAYS = tuple(min(0.002 * (2**n), 0.05) for n in range(_EMIT_MAX_ATTEMPTS))

# Substrings that must never appear in persisted payloads (defense in depth;
# services must redact earlier — this is the last line of defense).
_REDACT_KEYS = {"api_key", "apikey", "token", "secret", "password", "authorization", "cookie"}


def redact_payload(payload: dict[str, object]) -> dict[str, object]:
    """Shallow-redact obvious secret-bearing keys in an event payload."""
    cleaned: dict[str, object] = {}
    for key, value in payload.items():
        if any(marker in key.lower() for marker in _REDACT_KEYS):
            cleaned[key] = "[REDACTED]"
        else:
            cleaned[key] = value
    return cleaned


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[Subscriber]] = defaultdict(list)
        self._global_subscribers: list[Subscriber] = []
        self._lock = threading.Lock()
        self._sequence = 0

    def subscribe(self, event_type: str, callback: Subscriber) -> None:
        with self._lock:
            self._subscribers[event_type].append(callback)

    def subscribe_all(self, callback: Subscriber) -> None:
        with self._lock:
            self._global_subscribers.append(callback)

    def emit(self, event: Event, session: Session) -> Event:
        """Persist an event and notify subscribers. Assigns the sequence number.

        Duplicate `event_id` delivery is absorbed (idempotent emit): the
        previously persisted row is returned untouched, no sequence burned,
        no duplicate subscriber fanout (v3.1 §6 dedupe rule).

        Sequence races across bus instances (threads, worker processes) are
        absorbed the same way: two emitters can read the same max sequence
        and collide on the UNIQUE constraint. When the surrounding
        transaction holds nothing but this emit, the failed INSERT is rolled
        back, the counter reseeds from the new max, and the emit is retried —
        concurrent emitters converge instead of one of them failing the whole
        transaction. (A savepoint cannot be used here: in SQLAlchemy 2.0 a
        failed flush poisons the session past any savepoint, so only a full
        rollback of a caller-free transaction is a safe retry.) When the
        caller already has pending writes in the transaction, recovery would
        silently discard them, so the conflict propagates exactly as before —
        the caller's own retry then converges via dedupe or conditional
        claims.

        The event type is validated against the canonical taxonomy first: an
        unknown type raises rather than quietly persisting an event no
        consumer knows how to interpret.
        """
        validate_event_type(event.type)
        event = event.model_copy(update={"payload": redact_payload(event.payload)})
        # Snapshot caller state BEFORE flushing: after a flush we can no
        # longer tell caller writes apart from our own row, and only a
        # caller-free transaction may be rolled back for a retry.
        recoverable = not (session.new or session.dirty or session.deleted)
        # Caller writes go out first so they are visible to queries in this
        # transaction immediately; their errors propagate untouched.
        session.flush()
        last_error: IntegrityError | None = None
        for attempt in range(_EMIT_MAX_ATTEMPTS):
            if attempt > 0:
                time.sleep(_EMIT_RETRY_DELAYS[attempt])
            with self._lock:
                existing = session.get(EventRow, event.event_id)
                if existing is not None:
                    return self._row_to_event(existing)
                if self._sequence == 0:
                    row = session.execute(select(func.max(EventRow.sequence))).scalar_one_or_none()
                    self._sequence = int(row or 0)
                self._sequence += 1
                event = event.with_sequence(self._sequence)
                session.add(
                    EventRow(
                        event_id=event.event_id,
                        schema_version=event.schema_version,
                        session_id=event.session_id,
                        task_id=event.task_id,
                        agent_run_id=event.agent_run_id,
                        sequence=event.sequence,
                        timestamp=event.timestamp,
                        type=event.type,
                        actor=event.actor,
                        payload=event.payload,
                        visibility=EventVisibility(event.visibility),
                        sensitivity=EventSensitivity(event.sensitivity),
                    )
                )
                try:
                    session.flush()
                except IntegrityError as exc:
                    if not recoverable:
                        raise
                    # Nothing but our own uncommitted row is in this
                    # transaction: discard it, reseed from the new max, retry.
                    # The dedupe check above converges redeliveries.
                    last_error = exc
                    session.rollback()
                    self._sequence = 0
                    continue
                callbacks = list(self._global_subscribers) + list(self._subscribers[event.type])
            # Fan out outside the DB lock; subscriber exceptions never break emit.
            for callback in callbacks:
                try:
                    callback(event)
                except Exception:  # noqa: S110 — subscriber isolation is intentional
                    pass
            return event
        assert last_error is not None  # the loop only falls through via continue
        raise last_error

    def _row_to_event(self, r: EventRow) -> Event:
        return Event(
            event_id=r.event_id,
            schema_version=r.schema_version,
            session_id=r.session_id,
            task_id=r.task_id,
            agent_run_id=r.agent_run_id,
            sequence=r.sequence,
            timestamp=r.timestamp,
            type=r.type,
            actor=r.actor,
            payload=r.payload,
            visibility=EventVisibility(r.visibility),
            sensitivity=EventSensitivity(r.sensitivity),
        )

    def replay_after(
        self,
        session: Session,
        after_sequence: int,
        event_type: str | None = None,
        limit: int = 500,
        session_id: str | None = None,
    ) -> list[Event]:
        """Fetch persisted events after a sequence (resume-from-sequence, v3.1 §17)."""
        stmt = select(EventRow).where(EventRow.sequence > after_sequence)
        if event_type is not None:
            stmt = stmt.where(EventRow.type == event_type)
        if session_id is not None:
            stmt = stmt.where(EventRow.session_id == session_id)
        stmt = stmt.order_by(EventRow.sequence).limit(min(limit, 500))
        rows = session.execute(stmt).scalars().all()
        return [
            Event(
                event_id=r.event_id,
                schema_version=r.schema_version,
                session_id=r.session_id,
                task_id=r.task_id,
                agent_run_id=r.agent_run_id,
                sequence=r.sequence,
                timestamp=r.timestamp,
                type=r.type,
                actor=r.actor,
                payload=r.payload,
                visibility=EventVisibility(r.visibility),
                sensitivity=EventSensitivity(r.sensitivity),
            )
            for r in rows
        ]
