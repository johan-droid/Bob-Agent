"""Unit — event system invariants (v3.1 §6).

The architecture promises six properties; this suite proves each one against
the real EventBus + SQLite store (file DB, so durability is real):

    event_id globally unique ............ TestEventIdUniqueness
    sequence monotonic per session ...... TestSequenceOrdering
    same event x N = one logical UI event TestExactlyOnceUiEvent
    missing sequence = recoverable ....... TestGapRecovery
    out-of-order delivery = corrected .... TestOutOfOrderCorrection
    frontend reload = resumable .......... TestReloadResume

Dedupe-under-redelivery and resume-from-sequence are also exercised at the
socket level in tests/contract/test_realtime.py and
tests/recovery/test_failure_model.py; those prove the transports, these prove
the store invariants the transports stand on.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent_system.domain import ids
from agent_system.domain.events import Event
from agent_system.infra.db import make_engine, make_session_factory, session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Base, EventRow


def _open_db(path: Path) -> tuple[Any, Any]:
    engine = make_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    return engine, make_session_factory(engine)


@pytest.fixture()
def factory(tmp_path: Path) -> Any:
    engine, fac = _open_db(tmp_path / "events.db")
    yield fac
    engine.dispose()


def _emit(
    bus: EventBus,
    factory: Any,
    event_type: str = "task.created",
    session_id: str = "ses_inv",
    **kwargs: Any,
) -> Any:
    with session_scope(factory) as db:
        return bus.emit(Event(type=event_type, session_id=session_id, actor="t", **kwargs), db)


# ---------------------------------------------------------------------------
# event_id is globally unique
# ---------------------------------------------------------------------------


class TestEventIdUniqueness:
    def test_sequential_ids_unique_and_prefixed(self) -> None:
        seen = {ids.new_event_id() for _ in range(2000)}
        assert len(seen) == 2000
        assert all(i.startswith("evt_") for i in seen)

    def test_concurrent_id_generation_unique(self) -> None:
        results: list[list[str]] = [[] for _ in range(4)]
        barrier = threading.Barrier(4)

        def gen(n: int) -> None:
            barrier.wait(timeout=30)
            results[n] = [ids.new_event_id() for _ in range(250)]

        threads = [threading.Thread(target=gen, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        flat = [i for part in results for i in part]
        assert len(flat) == 1000 and len(set(flat)) == 1000

    def test_event_id_primary_key_rejects_duplicates(self, factory: Any) -> None:
        bus = EventBus()
        event = Event(type="task.created", session_id="ses_inv", actor="t")
        with session_scope(factory) as db:
            first = bus.emit(event, db)
            assert first.event_id.startswith("evt_")
            assert db.query(EventRow).count() == 1
        # Same logical event re-emitted: absorbed by id, never a second row.
        with session_scope(factory) as db:
            again = bus.emit(event, db)
            assert again.event_id == first.event_id
            assert again.sequence == first.sequence
            assert db.query(EventRow).count() == 1


# ---------------------------------------------------------------------------
# sequence is monotonically increasing per session
# ---------------------------------------------------------------------------


class TestSequenceOrdering:
    def test_per_session_sequences_strictly_increasing(self, factory: Any) -> None:
        bus = EventBus()
        sessions = ["ses_a", "ses_b", "ses_c"]
        per_session: dict[str, list[int]] = {s: [] for s in sessions}
        for _round_no in range(10):
            for session_id in sessions:
                emitted = _emit(bus, factory, session_id=session_id)
                per_session[session_id].append(emitted.sequence)
        for session_id, seqs in per_session.items():
            assert seqs == sorted(seqs), session_id
            assert len(set(seqs)) == len(seqs)
        all_seqs = [s for seqs in per_session.values() for s in seqs]
        assert len(set(all_seqs)) == len(all_seqs)  # globally unique too

    def test_per_session_order_survives_concurrency(self, factory: Any) -> None:
        barrier = threading.Barrier(3)
        errors: list[BaseException] = []

        def worker(session_id: str) -> None:
            bus = EventBus()
            try:
                barrier.wait(timeout=30)
                for _ in range(15):
                    _emit(bus, factory, session_id=session_id)
            except BaseException as exc:  # noqa: BLE001 — asserted empty below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(s,)) for s in ("x", "y", "z")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        assert errors == []
        with session_scope(factory) as db:
            for session_id in ("x", "y", "z"):
                rows = (
                    db.query(EventRow)
                    .filter_by(session_id=session_id)
                    .order_by(EventRow.sequence)
                    .all()
                )
                seqs = [r.sequence for r in rows]
                assert len(seqs) == 15
                assert seqs == sorted(seqs)
                assert len(set(seqs)) == 15


# ---------------------------------------------------------------------------
# same event delivered N times = one logical UI event
# ---------------------------------------------------------------------------


class TestExactlyOnceUiEvent:
    def test_ten_deliveries_replay_as_one(self, factory: Any) -> None:
        """What the UI renders comes from replay: N deliveries must replay as
        exactly one row, so N deliveries render exactly one UI event."""
        bus = EventBus()
        event = Event(type="task.created", session_id="ses_inv", task_id="t1", actor="t")
        with session_scope(factory) as db:
            for _ in range(10):
                bus.emit(event, db)
            replayed = bus.replay_after(db, after_sequence=0)
        assert [e.event_id for e in replayed] == [event.event_id]


# ---------------------------------------------------------------------------
# missing sequence = recoverable
# ---------------------------------------------------------------------------


class TestGapRecovery:
    def _insert_gap_row(self, factory: Any, sequence: int) -> None:
        """Simulate a sequence the client never saw (rolled-back mint, crash
        between mint and persist): a committed hole in the numbering."""
        with session_scope(factory) as db:
            db.add(
                EventRow(
                    event_id=f"evt_gap_{sequence}",
                    schema_version=1,
                    session_id="ses_inv",
                    task_id="gap",
                    sequence=sequence,
                    timestamp=datetime.now(UTC),
                    type="task.created",
                    actor="t",
                    payload={},
                    visibility="user",
                    sensitivity="normal",
                )
            )

    def test_replay_bridges_a_sequence_gap(self, factory: Any) -> None:
        bus = EventBus()
        first = _emit(bus, factory)
        second = _emit(bus, factory)
        self._insert_gap_row(factory, second.sequence + 3)  # hole: +1, +2 missing
        third = _emit(bus, factory)
        with session_scope(factory) as db:
            replayed = bus.replay_after(db, after_sequence=0)
            assert [e.sequence for e in replayed] == sorted(e.sequence for e in replayed)
            assert replayed[0].sequence == first.sequence
            # A client resume from `second` still receives everything after,
            # gap or not — nothing after the resume point is ever skipped,
            # always in sequence order (the mint order may differ from it).
            resumed = bus.replay_after(db, after_sequence=second.sequence)
            assert [e.sequence for e in resumed] == sorted([second.sequence + 3, third.sequence])


# ---------------------------------------------------------------------------
# out-of-order delivery = corrected (replay is always sequence-ordered)
# ---------------------------------------------------------------------------


class TestOutOfOrderCorrection:
    def test_concurrent_storm_replays_in_order(self, factory: Any) -> None:
        barrier = threading.Barrier(4)
        errors: list[BaseException] = []

        def worker(n: int) -> None:
            bus = EventBus()
            try:
                barrier.wait(timeout=30)
                for i in range(20):
                    _emit(
                        bus,
                        factory,
                        session_id=f"ses_{n}",
                        task_id=f"task_{n}_{i}",
                    )
            except BaseException as exc:  # noqa: BLE001 — asserted empty below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        assert errors == []
        bus = EventBus()
        with session_scope(factory) as db:
            replayed = bus.replay_after(db, after_sequence=0, limit=500)
        assert len(replayed) == 80
        seqs = [e.sequence for e in replayed]
        assert seqs == sorted(seqs)  # commit order may interleave; replay corrects
        assert len(set(seqs)) == 80


# ---------------------------------------------------------------------------
# frontend reload = resumable (the poll-then-follow pattern)
# ---------------------------------------------------------------------------


class TestReloadResume:
    def test_reload_catchup_poll_returns_only_new(self, factory: Any) -> None:
        """Reload: read latest-sequence, replay-since it (nothing new -> []),
        then later polls return exactly what arrived since."""
        bus = EventBus()
        first_batch = [_emit(bus, factory) for _ in range(3)]
        latest = max(e.sequence for e in first_batch)
        with session_scope(factory) as db:
            assert bus.replay_after(db, after_sequence=latest) == []
            second_batch = [_emit(bus, factory) for _ in range(2)]
            caught_up = bus.replay_after(db, after_sequence=latest)
        assert [e.sequence for e in caught_up] == [e.sequence for e in second_batch]
