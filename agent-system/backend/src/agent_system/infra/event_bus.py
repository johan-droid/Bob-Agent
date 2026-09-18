"""EventBus — the single canonical event pipeline (v3.1 §6).

Persists events to SQLite with a globally monotonic sequence number, fans out
to in-memory subscribers (WS/SSE bridges attach here in Phase 3/9). Redaction
of secrets happens at emit-time so sensitive values never enter the store.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from collections.abc import Callable

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from agent_system.domain.events import (
    Event,
    EventSensitivity,
    EventVisibility,
    validate_event_type,
)
from agent_system.infra.models import EventRow

Subscriber = Callable[[Event], None]

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


#: Collision-free sequence allocation: the sequence is computed *inside* the
#: INSERT, at statement-execute time. SQLite serializes writers from the first
#: write statement, so the inline subquery always observes the latest committed
#: max — no stale-snapshot mint, no UNIQUE violation even when two drivers
#: emit in the same instant with caller writes pending (a state the emit-level
#: retry cannot rescue: a caller-dirty transaction may not be rolled back).
#: On PostgreSQL the INSERT is preceded by ``pg_advisory_xact_lock`` (same
#: fixed key the claim seam uses): every event mint on every connection is
#: serialized, so the inline MAX subquery never races.
_PG_EMIT_LOCK = text("SELECT pg_advisory_xact_lock(:key)")
_PG_EMIT_LOCK_KEY = 721834917563402  # shared with orchestrator._PG_CLAIM_LOCK_KEY

_INSERT_WITH_SEQUENCE = text(
    "INSERT INTO events (event_id, schema_version, session_id, task_id, "
    "agent_run_id, sequence, timestamp, type, actor, payload, visibility, sensitivity) "
    "VALUES (:event_id, :schema_version, :session_id, :task_id, :agent_run_id, "
    "(SELECT COALESCE(MAX(sequence), 0) + 1 FROM events), "
    ":timestamp, :type, :actor, :payload, :visibility, :sensitivity)"
)


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

        Duplicate ``event_id`` delivery is absorbed (idempotent emit): the
        previously persisted row is returned untouched, no sequence burned,
        no duplicate subscriber fanout (v3.1 §6 dedupe rule).

        Sequence allocation is collision-free by construction: the sequence is
        computed *inside* the INSERT (``SELECT MAX(sequence) + 1`` at statement
        execute time), so two emitters in concurrent transactions can never
        mint the same number — SQLite serializes writers from the first write
        statement, and on PostgreSQL the task-transition seam holds
        ``pg_advisory_xact_lock`` across the same window. This matters because
        emitters here often carry caller writes (task transitions) in the same
        transaction, where a UNIQUE collision could not be retried: rolling
        back would discard the caller's state transition.

        The event type is validated against the canonical taxonomy first: an
        unknown type raises rather than quietly persisting an event no
        consumer knows how to interpret.
        """
        validate_event_type(event.type)
        event = event.model_copy(update={"payload": redact_payload(event.payload)})
        if session is None:
            # Memory / mock event bus emit when session is omitted
            self._sequence += 1
            event = event.with_sequence(self._sequence)
            callbacks = list(self._global_subscribers) + list(self._subscribers[event.type])
            for callback in callbacks:
                try:
                    callback(event)
                except Exception:
                    pass
            return event

        if session.get_bind().dialect.name == "postgresql":
            # Serialize ALL event mints across processes/connections (READ
            # COMMITTED would otherwise let two finishers mint MAX+1 twice).
            # Taken BEFORE flushing caller writes so every transaction follows
            # one lock order: advisory lock -> task rows -> events. No AB-BA.
            session.execute(_PG_EMIT_LOCK, {"key": _PG_EMIT_LOCK_KEY})
        # Caller writes go out first so they are visible to queries in this
        # transaction immediately; their errors propagate untouched.
        session.flush()
        # Dedupe check without holding self._lock: the DB commit below is the
        # real serialization point (event_id is the PRIMARY KEY; a duplicate
        # delivery would fail the INSERT, and the caller's retry converges).
        existing = session.get(EventRow, event.event_id)
        if existing is not None:
            return self._row_to_event(existing)
        session.execute(
            _INSERT_WITH_SEQUENCE,
            {
                "event_id": event.event_id,
                "schema_version": event.schema_version,
                "session_id": event.session_id,
                "task_id": event.task_id,
                "agent_run_id": event.agent_run_id,
                "timestamp": event.timestamp,
                "type": event.type,
                "actor": event.actor,
                "payload": json.dumps(event.model_dump(mode="json")["payload"]),
                "visibility": EventVisibility(event.visibility).value,
                "sensitivity": EventSensitivity(event.sensitivity).value,
            },
        )
        # Read back the allocated sequence; keep the in-memory counter warm.
        allocated = session.execute(
            select(EventRow.sequence).where(EventRow.event_id == event.event_id)
        ).scalar_one()
        event = event.with_sequence(int(allocated))
        self._sequence = max(self._sequence, int(allocated))
        callbacks = list(self._global_subscribers) + list(self._subscribers[event.type])
        # Fan out outside the DB lock; subscriber exceptions never break emit.
        for callback in callbacks:
            try:
                callback(event)
            except Exception:  # noqa: S110 — subscriber isolation is intentional
                pass
        return event

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
