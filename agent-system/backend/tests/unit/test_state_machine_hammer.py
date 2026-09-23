"""Unit — hammer the state machines: exactly one logical transition.

Every transition must satisfy::

    valid previous state + valid event + idempotency key + durable persistence
    = exactly one logical transition

So this suite hammers every transition path with duplicates and crashes and
pins that the logical effect happens exactly once:

    same request x2        (API double POST: create / transition / retry / decide)
    same event x10         (bus redelivery, same and across bus instances)
    concurrent delivery    (threads + barrier: claims must serialize)
    worker crash           (RUNNING + expired lease -> recover -> run -> dedup)
    API crash              (mid-transaction rollback; commit-then-kick-failure)
    retry / network retry  (frontend re-POST after a timeout)
"""

from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_system.agents import registry as agent_registry
from agent_system.api.main import app
from agent_system.domain.events import Event, utcnow
from agent_system.domain.lifecycles import AgentState
from agent_system.domain.tasks import TRANSITIONS as TASK_TRANSITIONS
from agent_system.domain.tasks import TaskState
from agent_system.domain.tasks import can_transition as can_transition_task
from agent_system.infra.db import make_engine, make_session_factory, session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import AgentLease, AgentRun, Base, EventRow, Task
from agent_system.services.orchestrator import Orchestrator, Supervisor
from agent_system.services.permissions import PermissionGate
from agent_system.services.tool_errors import NeedsApprovalError
from agent_system.services.tools.contract import TRANSITIONS as TOOL_TRANSITIONS
from agent_system.services.tools.contract import ToolLifecycle
from agent_system.services.tools.execution import execute_tool, run_tool_call
from agent_system.services.tools.protocol import parse_tool_calls
from agent_system.services.tools.registry import Tool, ToolContext, ToolRegistry


def _open_db(path: Path) -> tuple[Any, Any]:
    engine = make_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    return engine, make_session_factory(engine)


@pytest.fixture()
def factory(tmp_path: Path) -> Any:
    engine, fac = _open_db(tmp_path / "hammer.db")
    yield fac
    engine.dispose()


@pytest.fixture()
def client() -> Any:
    with TestClient(app) as c:
        token = c.app.state.authenticator.bootstrap_token  # type: ignore[attr-defined]
        c.headers["Authorization"] = f"Bearer {token}"
        yield c


@pytest.fixture()
def clean_registry() -> Any:
    saved = agent_registry.snapshot()
    yield
    agent_registry.restore(saved)


def _event_types(factory: Any, task_id: str | None = None) -> list[str]:
    with session_scope(factory) as db:
        query = db.query(EventRow)
        if task_id is not None:
            query = query.filter_by(task_id=task_id)
        return [row.type for row in query.all()]


def _task_state(factory: Any, task_id: str) -> tuple[str, int]:
    with session_scope(factory) as db:
        row = db.get(Task, task_id)
        assert row is not None
        return row.state, row.attempt


def _make_failed_task(client: Any) -> str:
    """A task walked to FAILED through the API (PENDING -> QUEUED -> RUNNING)."""
    session_id = client.post("/api/v1/sessions", json={"goal": "g"}).json()["id"]
    task_id = client.post(
        "/api/v1/tasks",
        json={"session_id": session_id, "task_type": "code", "title": "t"},
    ).json()["id"]
    for target in ("QUEUED", "RUNNING", "FAILED"):
        assert (
            client.post(f"/api/v1/tasks/{task_id}/transition", json={"target": target}).status_code
            == 200
        )
    return task_id


PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {"note": {"type": "string"}},
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# same event x10 — the bus absorbs redelivery by event id
# ---------------------------------------------------------------------------


class TestSameEventTimesTen:
    def test_same_event_ten_times_one_row_one_sequence_one_fanout(self, factory: Any) -> None:
        bus = EventBus()
        seen: list[str] = []
        bus.subscribe_all(lambda e: seen.append(e.event_id))
        event = Event(
            type="task.created", session_id="ses_h", task_id="task_h", actor="user", payload={}
        )
        with session_scope(factory) as db:
            first = bus.emit(event, db)
            for _ in range(9):
                dup = bus.emit(event, db)
                assert dup.event_id == event.event_id
                assert dup.sequence == first.sequence
            assert db.query(EventRow).count() == 1
        assert seen == [event.event_id]  # fanned out exactly once
        with session_scope(factory) as db:
            nxt = bus.emit(
                Event(type="task.queued", session_id="ses_h", task_id="task_h", actor="user"),
                db,
            )
            assert nxt.sequence == first.sequence + 1  # no sequence burned on dedupe

    def test_same_event_across_bus_instances_dedupes(self, factory: Any) -> None:
        """A restarted process (fresh bus, same DB) absorbs the redelivery."""
        event = Event(
            type="task.created", session_id="ses_h", task_id="task_h", actor="user", payload={}
        )
        bus1 = EventBus()
        with session_scope(factory) as db:
            first = bus1.emit(event, db)
        bus2 = EventBus()  # the "restarted" process
        with session_scope(factory) as db:
            for _ in range(3):
                assert bus2.emit(event, db).sequence == first.sequence
            assert db.query(EventRow).count() == 1
            nxt = bus2.emit(
                Event(type="task.queued", session_id="ses_h", task_id="task_h", actor="user"),
                db,
            )
            assert nxt.sequence == first.sequence + 1

    def test_invalid_event_type_rejected_before_persistence(self, factory: Any) -> None:
        from agent_system.domain.events import UnknownEventTypeError

        bus = EventBus()
        for _ in range(3):
            with session_scope(factory) as db:
                with pytest.raises(UnknownEventTypeError):
                    bus.emit(
                        Event(
                            type="task.made.up",
                            session_id="ses_h",
                            actor="user",
                        ),
                        db,
                    )
        with session_scope(factory) as db:
            assert db.query(EventRow).count() == 0


# ---------------------------------------------------------------------------
# concurrent emit — sequences stay unique across buses and threads
# ---------------------------------------------------------------------------


class TestConcurrentEmit:
    def test_concurrent_buses_unique_sequences(self, factory: Any) -> None:
        errors: list[BaseException] = []
        barrier = threading.Barrier(4)

        def worker(n: int) -> None:
            bus = EventBus()
            try:
                barrier.wait(timeout=30)
                for i in range(25):
                    with session_scope(factory) as db:
                        bus.emit(
                            Event(
                                type="task.created",
                                session_id="ses_h",
                                task_id=f"task_{n}_{i}",
                                actor="w",
                                payload={"n": n, "i": i},
                            ),
                            db,
                        )
            except BaseException as exc:  # noqa: BLE001 — collected, asserted below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        assert errors == []
        with session_scope(factory) as db:
            rows = db.query(EventRow).all()
            assert len(rows) == 100
            sequences = [r.sequence for r in rows]
            assert len(set(sequences)) == 100


# ---------------------------------------------------------------------------
# same request x2 — task creation with an idempotency key
# ---------------------------------------------------------------------------


class TestTaskCreateHammer:
    def _session(self, client: TestClient) -> str:
        return client.post("/api/v1/sessions", json={"goal": "g"}).json()["id"]

    def test_same_key_five_times_one_task(self, client: TestClient) -> None:
        body = {
            "session_id": self._session(client),
            "task_type": "code",
            "title": "once",
            "idempotency_key": "op:hammer-5x",
        }
        ids = [client.post("/api/v1/tasks", json=body).json()["id"] for _ in range(5)]
        assert len(set(ids)) == 1
        factory = client.app.state.session_factory
        with session_scope(factory) as db:
            assert db.query(Task).filter_by(idempotency_key="op:hammer-5x").count() == 1

    def test_concurrent_same_key_single_task(self, client: TestClient) -> None:
        """Two frontend retries racing: one task, both callers get its id."""
        body = {
            "session_id": self._session(client),
            "task_type": "code",
            "title": "race",
            "idempotency_key": "op:hammer-race",
        }
        barrier = threading.Barrier(2)
        results: list[tuple[int, str]] = []

        def post() -> None:
            barrier.wait(timeout=30)
            try:
                r = client.post("/api/v1/tasks", json=body)
                results.append((r.status_code, r.json()["id"]))
            except BaseException as exc:  # noqa: BLE001 — recorded, asserted below
                results.append((-1, f"{type(exc).__name__}"))

        threads = [threading.Thread(target=post) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        assert [s for s, _ in results] == [201, 201]
        assert len({i for _, i in results}) == 1
        factory = client.app.state.session_factory
        with session_scope(factory) as db:
            assert db.query(Task).filter_by(idempotency_key="op:hammer-race").count() == 1


# ---------------------------------------------------------------------------
# same request x2 — explicit transitions: redelivery never double-applies
# ---------------------------------------------------------------------------


class TestTransitionHammer:
    def _task(self, client: TestClient) -> str:
        session_id = client.post("/api/v1/sessions", json={"goal": "g"}).json()["id"]
        return client.post(
            "/api/v1/tasks",
            json={"session_id": session_id, "task_type": "code", "title": "t"},
        ).json()["id"]

    def test_same_target_twice_is_idempotent(self, client: TestClient) -> None:
        task_id = self._task(client)
        first = client.post(f"/api/v1/tasks/{task_id}/transition", json={"target": "QUEUED"})
        second = client.post(f"/api/v1/tasks/{task_id}/transition", json={"target": "QUEUED"})
        assert (first.status_code, second.status_code) == (200, 200)
        assert second.json()["state"] == "QUEUED"
        factory = client.app.state.session_factory
        state, attempt = _task_state(factory, task_id)
        assert (state, attempt) == ("QUEUED", 0)
        assert _event_types(factory, task_id).count("task.queued") == 1

    def test_concurrent_same_target_single_transition(self, client: TestClient) -> None:
        task_id = self._task(client)
        assert (
            client.post(f"/api/v1/tasks/{task_id}/transition", json={"target": "QUEUED"})
        ).status_code == 200
        barrier = threading.Barrier(2)
        statuses: list[int] = []

        def post() -> None:
            barrier.wait(timeout=30)
            r = client.post(f"/api/v1/tasks/{task_id}/transition", json={"target": "RUNNING"})
            statuses.append(r.status_code)

        threads = [threading.Thread(target=post) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        assert sorted(statuses) == [200, 200]
        factory = client.app.state.session_factory
        state, attempt = _task_state(factory, task_id)
        assert (state, attempt) == ("RUNNING", 1)  # entered RUNNING exactly once
        assert _event_types(factory, task_id).count("task.started") == 1

    def test_terminal_states_frozen_under_hammer(self, client: TestClient) -> None:
        walks = {
            "SUCCEEDED": ("QUEUED", "RUNNING", "SUCCEEDED"),
            "CANCELLED": ("CANCELLED",),
        }
        factory = client.app.state.session_factory
        for terminal, walk in walks.items():
            task_id = self._task(client)
            for target in walk:
                assert (
                    client.post(
                        f"/api/v1/tasks/{task_id}/transition", json={"target": target}
                    ).status_code
                    == 200
                )
            before = _event_types(factory, task_id)
            for target in TaskState:
                if target.value == terminal:
                    continue
                r = client.post(
                    f"/api/v1/tasks/{task_id}/transition", json={"target": target.value}
                )
                assert r.status_code == 409, (terminal, target)
            state, _ = _task_state(factory, task_id)
            assert state == terminal
            assert _event_types(factory, task_id) == before  # no transition, no event

    def test_failed_state_has_only_the_retry_exits(self, client: TestClient) -> None:
        """FAILED is terminal except for its narrow, explicit retry hatch:
        QUEUED (requeue) and RECOVERING (resume). Everything else 409s.
        Requesting FAILED itself is an idempotent no-op (already there)."""
        for target in TaskState:
            task_id = _make_failed_task(client)
            r = client.post(f"/api/v1/tasks/{task_id}/transition", json={"target": target.value})
            if target.value in ("QUEUED", "RECOVERING", "FAILED"):
                assert r.status_code == 200, target
                assert r.json()["state"] == target.value
            else:
                assert r.status_code == 409, target

    def test_task_machine_table_sweep(self) -> None:
        """Valid previous state is exhaustive: the table is the law, all 100 pairs."""
        for current in TaskState:
            for target in TaskState:
                assert can_transition_task(current, target) is (
                    target in TASK_TRANSITIONS[current]
                ), (current, target)


# ---------------------------------------------------------------------------
# retry / network retry — redelivery re-kicks but never double-applies
# ---------------------------------------------------------------------------


class TestRetryHammer:
    def _failed_task(self, client: TestClient) -> str:
        return _make_failed_task(client)

    def test_retry_twice_second_rekicks_without_requeue(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kicked: list[str] = []
        monkeypatch.setattr(
            "agent_system.services.task_runner.kick_task",
            lambda factory, bus, task_id, settings=None: kicked.append(task_id) or True,
        )
        task_id = self._failed_task(client)
        first = client.post(f"/api/v1/tasks/{task_id}/retry")
        second = client.post(f"/api/v1/tasks/{task_id}/retry")
        assert (first.status_code, second.status_code) == (202, 200)
        assert second.json()["state"] == "QUEUED"
        assert kicked == [task_id, task_id]  # execution ensured twice, queued once
        factory = client.app.state.session_factory
        state, attempt = _task_state(factory, task_id)
        assert (state, attempt) == ("QUEUED", 1)  # requeue never increments
        # One task.queued from the lifecycle walk, exactly one from the retry.
        assert _event_types(factory, task_id).count("task.queued") == 2

    def test_concurrent_retry_single_requeue(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kicked: list[str] = []
        monkeypatch.setattr(
            "agent_system.services.task_runner.kick_task",
            lambda factory, bus, task_id, settings=None: kicked.append(task_id) or True,
        )
        task_id = self._failed_task(client)
        barrier = threading.Barrier(2)
        statuses: list[int] = []

        def post() -> None:
            barrier.wait(timeout=30)
            statuses.append(client.post(f"/api/v1/tasks/{task_id}/retry").status_code)

        threads = [threading.Thread(target=post) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        assert sorted(statuses) == [200, 202]
        factory = client.app.state.session_factory
        assert _task_state(factory, task_id) == ("QUEUED", 1)
        # One task.queued from the lifecycle walk, exactly one from the retry.
        assert _event_types(factory, task_id).count("task.queued") == 2

    def test_kick_failure_then_retry_recovers(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """API crash after commit, before kick: 503 leaves QUEUED work that a
        later retry re-kicks instead of 409ing as 'already queued'."""
        calls = {"n": 0}

        def flaky_kick(factory: Any, bus: Any, task_id: str, settings: Any = None) -> bool:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("kick crashed")
            return True

        monkeypatch.setattr("agent_system.services.task_runner.kick_task", flaky_kick)
        task_id = self._failed_task(client)
        assert client.post(f"/api/v1/tasks/{task_id}/retry").status_code == 503
        factory = client.app.state.session_factory
        assert _task_state(factory, task_id)[0] == "QUEUED"  # commit survived the crash
        retry = client.post(f"/api/v1/tasks/{task_id}/retry")
        assert retry.status_code == 200
        assert retry.json()["state"] == "QUEUED"
        assert calls["n"] == 2

    def test_sweep_backlog_rekicks_after_crash(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_system.services.task_runner import sweep_backlog

        kicked: list[str] = []
        monkeypatch.setattr(
            "agent_system.services.task_runner.kick_task",
            lambda factory, bus, task_id, settings=None: kicked.append(task_id) or True,
        )
        task_id = self._failed_task(client)
        factory = client.app.state.session_factory
        bus = client.app.state.event_bus
        with session_scope(factory) as db:
            row = db.get(Task, task_id)
            assert row is not None
            row.state = TaskState.QUEUED.value  # left QUEUED by the crashed process
        assert sweep_backlog(factory, bus) == [task_id]
        assert kicked == [task_id]


# ---------------------------------------------------------------------------
# worker crash + duplicate delivery — the claim is the idempotency key
# ---------------------------------------------------------------------------


class TestWorkerHammer:
    def _queued(self, factory: Any) -> tuple[str, str]:
        bus = EventBus()
        sup = Supervisor(bus)
        session_id = sup.create_session(factory, "hammer")
        task_id = sup.add_task(factory, session_id, "code", "work")
        with session_scope(factory) as db:
            row = db.get(Task, task_id)
            assert row is not None
            row.state = TaskState.QUEUED.value
        return session_id, task_id

    def test_redelivery_skipped_five_times(self, factory: Any) -> None:
        _, task_id = self._queued(factory)
        with session_scope(factory) as db:
            row = db.get(Task, task_id)
            assert row is not None
            row.state = TaskState.RUNNING.value
            row.attempt = 1
        orch = Orchestrator(EventBus())
        for _ in range(5):
            assert orch._run_task(factory, task_id) is False
        with session_scope(factory) as db:
            assert db.query(AgentRun).filter_by(task_id=task_id).count() == 0
            row = db.get(Task, task_id)
            assert row is not None and row.attempt == 1

    def test_concurrent_claims_single_execution(self, factory: Any, clean_registry: Any) -> None:
        """Four duplicate deliveries racing: one claim wins and runs the
        handler once; the losers skip."""
        _, task_id = self._queued(factory)
        ran: list[str] = []
        bus = EventBus()
        orch = Orchestrator(bus)
        orch.register_handler("code", lambda i, c: ran.append(task_id) or {"ok": True})
        barrier = threading.Barrier(4)
        outcomes: list[bool] = []

        def run() -> None:
            barrier.wait(timeout=30)
            try:
                res = orch._run_task(factory, task_id)
                outcomes.append(res)
            except BaseException:  # noqa: BLE001 — any failure is a bug
                outcomes.append(False)

        threads = [threading.Thread(target=run) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=180)
        assert sorted(outcomes) == [False, False, False, True]
        assert ran == [task_id]
        with session_scope(factory) as db:
            assert db.query(AgentRun).filter_by(task_id=task_id).count() == 1
            row = db.get(Task, task_id)
            assert row is not None and row.attempt == 1
        assert _event_types(factory, task_id).count("task.started") == 1

    def test_crash_recover_run_redeliver_chain(self, factory: Any, clean_registry: Any) -> None:
        bus = EventBus()
        orch = Orchestrator(bus)
        orch.register_handler("code", lambda i, c: {"ok": True})
        _, task_id = self._queued(factory)
        run_id = "run_hammer_crash"
        with session_scope(factory) as db:
            row = db.get(Task, task_id)
            assert row is not None
            row.state = TaskState.RUNNING.value
            row.attempt = 1
            db.add(
                AgentRun(
                    id=run_id,
                    task_id=task_id,
                    agent_type="code",
                    state="RUNNING",
                    worker_id="dead",
                )
            )
            db.add(
                AgentLease(
                    agent_run_id=run_id,
                    worker_id="dead",
                    state="RUNNING",
                    heartbeat_at=utcnow() - timedelta(minutes=5),
                    lease_expires_at=utcnow() - timedelta(minutes=4),
                )
            )
        assert orch.recover_orphans(factory) == [task_id]  # worker crash -> requeue
        assert orch.recover_orphans(factory) == []  # reaper itself is idempotent
        ran = orch._run_task(factory, task_id)  # runs once, succeeds
        assert ran is True
        assert orch._run_task(factory, task_id) is False  # redelivery
        with session_scope(factory) as db:
            assert db.query(AgentRun).filter_by(task_id=task_id).count() == 2  # dead + real
            row = db.get(Task, task_id)
            assert row is not None and row.state == TaskState.SUCCEEDED.value


# ---------------------------------------------------------------------------
# orchestrator claim — concurrent drivers serialize on the task row
# ---------------------------------------------------------------------------


class TestOrchestratorClaimHammer:
    def test_concurrent_run_task_single_execution(self, factory: Any) -> None:
        bus = EventBus()
        sup, orch = Supervisor(bus), Orchestrator(bus)
        session_id = sup.create_session(factory, "hammer")
        task_id = sup.add_task(factory, session_id, "code", "work")
        with session_scope(factory) as db:
            row = db.get(Task, task_id)
            assert row is not None
            row.state = TaskState.QUEUED.value
        entered: list[str] = []
        orch.register_handler("code", lambda i, c: entered.append(task_id) or {"ok": True})
        barrier = threading.Barrier(4)
        results: list[bool] = []

        def run() -> None:
            barrier.wait(timeout=30)
            results.append(orch._run_task(factory, task_id))

        threads = [threading.Thread(target=run) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=180)
        assert sorted(results) == [False, False, False, True]
        assert entered == [task_id]
        with session_scope(factory) as db:
            assert db.query(AgentRun).filter_by(task_id=task_id).count() == 1
            row = db.get(Task, task_id)
            assert row is not None and (row.state, row.attempt) == ("SUCCEEDED", 1)


# ---------------------------------------------------------------------------
# approval hammer — first decision sticks, one decision one event
# ---------------------------------------------------------------------------


class TestApprovalHammer:
    def test_decide_three_times_single_event_first_sticks(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/approvals",
            json={
                "requested_action": "fs.delete",
                "risk": "MEDIUM",
                "scope": "file:delete",
                "requester": "CodeAgent",
            },
        )
        assert r.status_code == 202
        approval_id = r.json()["approval_id"]
        bodies = [
            {"approve": True},
            {"approve": True, "reason": "retry after timeout"},
            {"approve": False, "reason": "second thoughts"},
        ]
        for body in bodies:
            d = client.post(f"/api/v1/approvals/{approval_id}/decision", json=body)
            assert d.status_code == 200
            assert d.json()["decision"] == "APPROVED"
        factory = client.app.state.session_factory
        types = _event_types(factory)
        assert types.count("approval.approved") == 1
        assert "approval.denied" not in types

    def test_allow_once_grant_single_execution_under_redelivery(self, factory: Any) -> None:
        """The grant is the idempotency key: redelivering the call waits, never reruns."""
        ran: list[dict[str, Any]] = []
        reg = ToolRegistry()

        def gated(args: dict[str, Any], _ctx: Any) -> dict[str, Any]:
            ran.append(args)
            return {"wrote": True}

        reg.register(Tool("gated", "gated", PARAMS, "write", gated, group="test"))
        tool = reg.get("gated")
        assert tool is not None
        gate = PermissionGate()
        ctx = ToolContext(
            settings=SimpleNamespace(tools_require_approval=True),
            gate=gate,
            session_id="ses_h",
            task_id="task_h",
            agent_run_id="run_h",
            agent_type="llm",
        )
        with pytest.raises(NeedsApprovalError):
            execute_tool(tool, {"note": "hi"}, ctx)
        gate.decide(gate.pending()[0].approval_id, approve=True)
        assert execute_tool(tool, {"note": "hi"}, ctx) == {"wrote": True}
        with pytest.raises(NeedsApprovalError):  # grant consumed: redelivery waits
            execute_tool(tool, {"note": "hi"}, ctx)
        assert ran == [{"note": "hi"}]


# ---------------------------------------------------------------------------
# API crash — transactions are all-or-nothing; the reaper is idempotent
# ---------------------------------------------------------------------------


class TestCrashAtomicity:
    def test_crash_mid_transaction_rolls_back(self, factory: Any) -> None:
        bus = EventBus()
        sup = Supervisor(bus)
        session_id = sup.create_session(factory, "atomic")
        task_id = sup.add_task(factory, session_id, "code", "work")
        with session_scope(factory) as db:
            row = db.get(Task, task_id)
            assert row is not None
            row.state = TaskState.QUEUED.value
        with pytest.raises(RuntimeError, match="boom"):
            with session_scope(factory) as db:
                row = db.get(Task, task_id)
                assert row is not None
                row.state = TaskState.RUNNING.value
                row.attempt += 1
                bus.emit(
                    Event(
                        type="task.started",
                        session_id=session_id,
                        task_id=task_id,
                        actor="code",
                    ),
                    db,
                )
                raise RuntimeError("boom")  # API crash before commit
        state, attempt = _task_state(factory, task_id)
        assert (state, attempt) == ("QUEUED", 0)
        assert "task.started" not in _event_types(factory, task_id)

    def test_recover_orphans_twice_second_is_noop(self, factory: Any) -> None:
        bus = EventBus()
        sup, orch = Supervisor(bus), Orchestrator(bus)
        session_id = sup.create_session(factory, "reap")
        task_id = sup.add_task(factory, session_id, "code", "work")
        with session_scope(factory) as db:
            row = db.get(Task, task_id)
            assert row is not None
            row.state = TaskState.RUNNING.value
            row.attempt = 1
            db.add(
                AgentRun(
                    id="run_hammer_reap",
                    task_id=task_id,
                    agent_type="code",
                    state="RUNNING",
                    worker_id="dead",
                )
            )
            db.add(
                AgentLease(
                    agent_run_id="run_hammer_reap",
                    worker_id="dead",
                    state="RUNNING",
                    heartbeat_at=utcnow() - timedelta(minutes=5),
                    lease_expires_at=utcnow() - timedelta(minutes=4),
                )
            )
        assert orch.recover_orphans(factory) == [task_id]
        assert orch.recover_orphans(factory) == []
        assert _task_state(factory, task_id) == ("QUEUED", 1)
        assert _event_types(factory, task_id).count("recovery.started") == 1


# ---------------------------------------------------------------------------
# tool lifecycle hammer — terminal lifecycles are frozen; deny never runs
# ---------------------------------------------------------------------------


class TestToolLifecycleHammer:
    def test_tool_machine_table_sweep(self) -> None:
        from agent_system.services.tools.contract import can_transition

        for current in ToolLifecycle:
            for target in ToolLifecycle:
                assert can_transition(current, target) is (target in TOOL_TRANSITIONS[current])
        for terminal in (
            ToolLifecycle.REJECTED,
            ToolLifecycle.SUCCEEDED,
            ToolLifecycle.FAILED,
        ):
            assert TOOL_TRANSITIONS[terminal] == frozenset()

    def test_agent_machine_terminals_frozen(self) -> None:
        from agent_system.domain.lifecycles import TRANSITIONS, can_transition

        for terminal in (AgentState.COMPLETED, AgentState.TERMINATED):
            assert TRANSITIONS[terminal] == frozenset()
            for target in AgentState:
                assert not can_transition(terminal, target)

    def test_destructive_refused_twice_never_runs(self, factory: Any) -> None:
        ran: list[dict[str, Any]] = []
        reg = ToolRegistry()

        def doom(args: dict[str, Any], _ctx: Any) -> dict[str, Any]:
            ran.append(args)
            return {"doomed": True}

        reg.register(Tool("doom", "doom", PARAMS, "destructive", doom, group="test"))
        ctx = ToolContext(
            settings=SimpleNamespace(tools_require_approval=False),
            session_id="ses_h",
            task_id="task_h",
            agent_run_id="run_h",
            agent_type="llm",
        )
        for _ in range(2):
            calls = parse_tool_calls("```tool:doom\n{}\n```")
            results = [run_tool_call(call, reg, ctx) for call in calls]
            assert len(results) == 1 and results[0].refused
        assert ran == []
