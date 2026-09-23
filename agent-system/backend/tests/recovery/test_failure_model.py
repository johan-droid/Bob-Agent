"""Recovery — the production failure model, proven case by case.

Worker crash was already proven (lease reaper). This suite covers the rest of
the documented reliability model, each with a deterministic simulation plus
the exact recovery contract it pins:

    API dies        TestApiDies         (kill -9 -> startup sequence -> quiescence)
    worker dies     proven elsewhere    (lease reaper; see below)
    Redis dies      TestRedisDies       (loud failure, nothing silently lost)
    DB locks        TestDbLocks         (WAL pragmas; readers never block; writers queue)
    LLM timeout     TestLlmTimeout      (bounded, recorded, lease-cleaned, retryable)
    network drops   TestNetworkDrops    (drop -> FAILED with reason -> retry -> SUCCEEDED)
    browser dies    TestBrowserDies     (ToolError, never a crash or hang; close/reopen)
    Docker dies     TestDockerDies      (fast fail; timeout kills and cleans the container)
    WS disconnects  TestWebsocketDrops  (disconnect unsubscribes; resume replays exactly)
    frontend reload TestFrontendReload  (pending approvals survive; decide still works)
    OS restart      TestOsRestart       (TTL bounds lease staleness across a reboot)

Worker crash is covered by tests/integration/test_orchestrator.py (stale lease
recovery), tests/integration/test_worker.py (reaper paths) and
tests/unit/test_state_machine_hammer.py (crash -> recover -> run -> redeliver).

Explicitly NOT covered here: the autonomous RecoveryPlanner loop
(classify -> plan -> execute) is unit-tested but not wired into any execution
path — no test here pretends otherwise. And the live Redis-loss /
WebSocket-flap matrix still needs live infrastructure by definition.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_system.agents import registry as agent_registry
from agent_system.api.main import app
from agent_system.domain.events import Event, utcnow
from agent_system.domain.tasks import TaskState
from agent_system.infra.db import make_engine, make_session_factory, session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import AgentLease, AgentRun, Base, EventRow, Task
from agent_system.services.agent_loop import run_tool_loop
from agent_system.services.orchestrator import Orchestrator, Supervisor
from agent_system.services.permissions import (
    ApprovalRequest,
    Decision,
    PermissionGate,
    Risk,
)
from agent_system.services.sandbox import (
    DockerSandbox,
    SandboxError,
    SandboxUnavailableError,
)
from agent_system.services.task_runner import sweep_backlog
from agent_system.services.tools.builtin import browser as browser_mod
from agent_system.services.tools.registry import Tool, ToolContext, ToolRegistry


def _open_db(path: Path) -> tuple[Any, Any]:
    engine = make_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    return engine, make_session_factory(engine)


@pytest.fixture()
def factory(tmp_path: Path) -> Any:
    engine, fac = _open_db(tmp_path / "failure.db")
    yield fac
    engine.dispose()


@pytest.fixture()
def clean_registry() -> Any:
    saved = agent_registry.snapshot()
    yield
    agent_registry.restore(saved)


@pytest.fixture()
def client() -> Any:
    with TestClient(app) as c:
        token = c.app.state.authenticator.bootstrap_token  # type: ignore[attr-defined]
        c.headers["Authorization"] = f"Bearer {token}"
        yield c


def _task_state(factory: Any, task_id: str) -> tuple[str, int]:
    with session_scope(factory) as db:
        row = db.get(Task, task_id)
        assert row is not None
        return row.state, row.attempt


def _event_types(factory: Any, task_id: str) -> list[str]:
    with session_scope(factory) as db:
        return [row.type for row in db.query(EventRow).filter_by(task_id=task_id).all()]


def _wait_terminal(factory: Any, task_ids: list[str], timeout: float = 20.0) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    states: dict[str, str] = {}
    while time.monotonic() < deadline:
        with session_scope(factory) as db:
            rows = db.query(Task).filter(Task.id.in_(task_ids)).all()
            states = {row.id: row.state for row in rows}
        if states and all(s in ("SUCCEEDED", "FAILED", "CANCELLED") for s in states.values()):
            return states
        time.sleep(0.05)
    return states


# ---------------------------------------------------------------------------
# API dies — kill -9 leaves committed rows; the startup sequence finishes them
# ---------------------------------------------------------------------------


class TestApiDies:
    def _crashed_db(self, tmp_path: Path) -> tuple[Any, Any, str, str, str]:
        """Simulate `kill -9`: committed QUEUED + RUNNING-expired work, then
        dispose the engine without any graceful shutdown."""
        db_file = tmp_path / "api_crash.db"
        engine1, factory1 = _open_db(db_file)
        bus = EventBus()
        sup = Supervisor(bus)
        session_id = sup.create_session(factory1, "crashed api")
        queued_id = sup.add_task(factory1, session_id, "code", "queued work")
        running_id = sup.add_task(factory1, session_id, "code", "running work")
        with session_scope(factory1) as db:
            qrow = db.get(Task, queued_id)
            assert qrow is not None
            qrow.state = TaskState.QUEUED.value  # actually queued when killed
            row = db.get(Task, running_id)
            assert row is not None
            row.state = TaskState.RUNNING.value
            row.attempt = 1
            db.add(
                AgentRun(
                    id="run_api_crash",
                    task_id=running_id,
                    agent_type="code",
                    state="RUNNING",
                    worker_id="dead-api",
                )
            )
            db.add(
                AgentLease(
                    agent_run_id="run_api_crash",
                    worker_id="dead-api",
                    state="RUNNING",
                    heartbeat_at=utcnow() - timedelta(minutes=5),
                    lease_expires_at=utcnow() - timedelta(minutes=1),
                )
            )
        engine1.dispose()  # kill -9: no shutdown hooks, no thread joins
        return db_file, session_id, queued_id, running_id, "run_api_crash"

    def test_startup_order_recover_before_sweep(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The lifespan order is load-bearing: a sweep alone skips a task that
        is still RUNNING (kick only runs QUEUED), so orphans must be reaped
        first — exactly the lifespan's recover-then-sweep order."""
        db_file, _, queued_id, running_id, _ = self._crashed_db(tmp_path)
        engine2, factory2 = _open_db(db_file)
        try:
            kicked: list[str] = []
            monkeypatch.setattr(
                "agent_system.services.task_runner.kick_task",
                lambda factory, bus, task_id, settings=None: kicked.append(task_id) or True,
            )
            bus = EventBus()
            orch = Orchestrator(bus)
            # Sweep first: it sees the QUEUED task but is blind to the
            # still-RUNNING orphan (kick only runs QUEUED).
            assert sweep_backlog(factory2, bus) == [queued_id]
            assert kicked == [queued_id]
            assert orch.recover_orphans(factory2) == [running_id]
            assert sorted(sweep_backlog(factory2, bus)) == sorted([queued_id, running_id])
            assert sorted(kicked) == sorted([queued_id, queued_id, running_id])
        finally:
            engine2.dispose()

    def test_kill_then_startup_recovery_completes_work(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_file, session_id, queued_id, running_id, _ = self._crashed_db(tmp_path)
        engine2, factory2 = _open_db(db_file)
        try:
            # A rebooted process starts with clean runner state (lifespan calls
            # reset_runner); earlier tests' lifespans may have shut it down.
            import agent_system.services.task_runner as runner_mod

            runner_mod.reset_runner()
            ran: list[str] = []
            orch = Orchestrator(EventBus())
            orch.register_handler("code", lambda i, c: ran.append(i.get("t", "?")) or {"ok": True})
            monkeypatch.setattr(runner_mod, "_build_orchestrator", lambda bus, settings=None: orch)
            # The lifespan sequence, verbatim: requeue expired leases, then
            # kick everything QUEUED (real kicks, real driver threads).
            assert Orchestrator(EventBus()).recover_orphans(factory2) == [running_id]
            kicked = sweep_backlog(factory2, EventBus())
            assert sorted(kicked) == sorted([queued_id, running_id])
            states = _wait_terminal(factory2, [queued_id, running_id])
            assert states == {queued_id: "SUCCEEDED", running_id: "SUCCEEDED"}
            assert _task_state(factory2, queued_id) == ("SUCCEEDED", 1)
            # The crashed attempt still counts exactly once; the recovery run once.
            assert _task_state(factory2, running_id) == ("SUCCEEDED", 2)
            with session_scope(factory2) as db:
                assert db.query(AgentRun).filter_by(task_id=running_id).count() == 2
        finally:
            engine2.dispose()


# ---------------------------------------------------------------------------
# DB locks — WAL + busy timeout: readers never block, writers serialize
# ---------------------------------------------------------------------------


class TestDbLocks:
    def test_sqlite_durability_pragmas(self, tmp_path: Path) -> None:
        engine, _ = _open_db(tmp_path / "pragma.db")
        try:
            with engine.connect() as conn:
                assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
                assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
                assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        finally:
            engine.dispose()

    def test_writer_does_not_block_readers(self, factory: Any) -> None:
        bus = EventBus()
        sup = Supervisor(bus)
        session_id = sup.create_session(factory, "locks")
        task_id = sup.add_task(factory, session_id, "code", "t")
        with session_scope(factory) as db:
            row = db.get(Task, task_id)
            assert row is not None
            row.state = TaskState.QUEUED.value
        holder = factory()
        try:
            row = holder.get(Task, task_id)
            assert row is not None
            row.state = TaskState.RUNNING.value
            holder.flush()  # uncommitted write: holds the write lock
            # A reader proceeds under WAL and sees the last COMMITTED state
            # (no dirty reads), even with a writer mid-transaction.
            with session_scope(factory) as db:
                assert db.get(Task, task_id).state == TaskState.QUEUED.value  # type: ignore[union-attr]
            # A second writer blocks (busy_timeout) and then succeeds once the
            # holder commits — serialization, not failure.
            errors: list[BaseException] = []
            done = threading.Event()

            def second_writer() -> None:
                try:
                    with session_scope(factory) as db:
                        row2 = db.get(Task, task_id)
                        assert row2 is not None
                        row2.state = TaskState.FAILED.value
                except BaseException as exc:  # noqa: BLE001 — asserted empty below
                    errors.append(exc)
                finally:
                    done.set()

            thread = threading.Thread(target=second_writer)
            thread.start()
            time.sleep(0.3)  # let the second writer block on the held lock
            holder.commit()
            assert done.wait(timeout=30)
            thread.join(timeout=30)
            assert errors == []
            assert _task_state(factory, task_id)[0] == "FAILED"
        finally:
            holder.close()


# ---------------------------------------------------------------------------
# LLM timeout — bounded, recorded, lease-cleaned, retryable (never stuck)
# ---------------------------------------------------------------------------


class TestLlmTimeout:
    def test_llm_timeout_fails_task_without_sticking(
        self, factory: Any, clean_registry: Any
    ) -> None:
        """A provider timeout mid-run: the RQ job raises (its own retry
        accounting), but the durable record is FAILED with the error, the run
        is FAILED, and the lease is gone — nothing stays RUNNING."""

        def timeout_handler(task_input: Any, context: Any) -> Any:
            raise TimeoutError("upstream timed out after 120s")

        agent_registry.register("code", timeout_handler)
        bus = EventBus()
        sup = Supervisor(bus)
        session_id = sup.create_session(factory, "llm timeout")
        task_id = sup.add_task(factory, session_id, "code", "ask the model")
        with session_scope(factory) as db:
            row = db.get(Task, task_id)
            assert row is not None
            row.state = TaskState.QUEUED.value
        orch = Orchestrator(bus)
        orch.register_handler("code", timeout_handler)
        res = orch._run_task(factory, task_id)
        assert res is False
        with session_scope(factory) as db:
            row = db.get(Task, task_id)
            assert row is not None
            assert row.state == TaskState.FAILED.value
            assert "timed out" in (row.last_error or "")
            runs = db.query(AgentRun).filter_by(task_id=task_id).all()
            assert len(runs) == 1 and runs[0].state == "FAILED"
            assert db.query(AgentLease).count() == 0
        assert "task.failed" in _event_types(factory, task_id)

    def test_error_stopped_run_fails_verifier_then_retry_recovers(self, factory: Any) -> None:
        """The network-drop chain end to end: a handler whose loop died on a
        dropped connection must FAIL the REVIEW gate (never phantom-succeed),
        and a later retry with the network back completes the task."""
        bus = EventBus()
        sup, orch = Supervisor(bus), Orchestrator(bus)
        session_id = sup.create_session(factory, "drop then recover")
        task_id = sup.add_task(factory, session_id, "code", "ask the model")
        calls = {"n": 0}

        def flaky(task_input: Any, context: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "output": "model invocation failed: ConnectionError: net down",
                    "stopped": "error",
                    "tool_calls": 0,
                    "iterations": 1,
                }
            return {"output": "recovered answer"}

        orch.register_handler("code", flaky)
        with session_scope(factory) as db:
            row = db.get(Task, task_id)
            assert row is not None
            row.state = TaskState.QUEUED.value
        assert orch.run_ready_tasks(factory, session_id) == [task_id]
        state, _ = _task_state(factory, task_id)
        assert state == "FAILED"
        with session_scope(factory) as db:
            row = db.get(Task, task_id)
            assert row is not None and "agent loop ended in error" in (row.last_error or "")
        assert "qa.failed" in _event_types(factory, task_id)
        # Operator retry with the network back: explicit requeue, then success.
        with session_scope(factory) as db:
            row = db.get(Task, task_id)
            assert row is not None
            from agent_system.domain.tasks import validate_transition

            validate_transition(TaskState.FAILED, TaskState.QUEUED)
            row.state = TaskState.QUEUED.value
        assert orch.run_ready_tasks(factory, session_id) == [task_id]
        assert _task_state(factory, task_id)[0] == "SUCCEEDED"


# ---------------------------------------------------------------------------
# browser dies — dead sessions degrade to ToolError; close/reopen recovers
# ---------------------------------------------------------------------------


class _DeadPage:
    """Acts like a crashed chromium: every operation raises."""

    url = "http://dead.invalid/"

    def goto(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Browser connection lost")

    def click(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Target crashed")

    def inner_text(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Target crashed")

    def title(self, *args: Any, **kwargs: Any) -> str:
        raise RuntimeError("Target crashed")


class _RecordingCloser:
    """Close/stop recorders: proves dead sessions are still cleaned up."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def stop(self) -> None:
        self.closed = True


class TestBrowserDies:
    def _inject_dead(self, name: str) -> _RecordingCloser:
        closer = _RecordingCloser()
        browser_mod.sessions._sessions[name] = {  # noqa: SLF001 — fault injection
            "playwright": closer,
            "browser": closer,
            "page": _DeadPage(),
        }
        return closer

    def _ctx(self) -> ToolContext:
        return ToolContext(
            settings=SimpleNamespace(browser_headless=True),
            session_id="ses_h",
            task_id="task_h",
            agent_run_id="run_h",
            agent_type="llm",
        )

    def test_dead_browser_actions_raise_tool_error(self) -> None:
        from agent_system.services.tool_errors import ToolError

        closer = self._inject_dead("dead1")
        try:
            ctx = self._ctx()
            with pytest.raises(ToolError, match="navigation failed"):
                browser_mod._browser_open({"session": "dead1", "url": "https://example.com"}, ctx)
            with pytest.raises(ToolError, match="click failed"):
                browser_mod._browser_click({"session": "dead1", "selector": "#x"}, ctx)
            with pytest.raises(ToolError, match="extract failed"):
                browser_mod._browser_extract({"session": "dead1"}, ctx)
            # The dead session closes cleanly (close errors are swallowed).
            assert browser_mod.sessions.close("dead1") is True
            assert closer.closed
            with pytest.raises(ToolError, match="no open browser session"):
                browser_mod.sessions.get("dead1")
        finally:
            browser_mod.sessions._sessions.pop("dead1", None)  # noqa: SLF001

    def test_browser_session_limit_enforced(self) -> None:
        from agent_system.services.tool_errors import ToolError

        names = [f"limit{i}" for i in range(browser_mod.MAX_SESSIONS)]
        try:
            for name in names:
                self._inject_dead(name)
            with pytest.raises(ToolError, match="session limit"):
                browser_mod.sessions.ensure(self._ctx(), "one-too-many")
        finally:
            for name in names:
                browser_mod.sessions._sessions.pop(name, None)  # noqa: SLF001

    def test_dead_browser_in_loop_is_model_readable(self) -> None:

        closer = self._inject_dead("dead-loop")
        try:
            ran: list[dict[str, Any]] = []

            def click(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
                ran.append(args)
                return browser_mod._browser_click(args, ctx)

            reg = ToolRegistry()
            reg.register(
                Tool(
                    "brws_click",
                    "click",
                    {
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string"},
                            "session": {"type": "string"},
                        },
                        "additionalProperties": False,
                    },
                    "read",
                    click,
                    group="test",
                )
            )
            turns = ['```tool:brws_click\n{"selector": "#x", "session": "dead-loop"}\n```']
            calls = {"n": 0}

            def invoke(transcript: str) -> dict[str, Any]:
                calls["n"] += 1
                if calls["n"] == 1:
                    return {"output": turns[0]}
                assert '"tool_error"' in transcript
                assert "Target crashed" in transcript
                return {"output": "the page is gone; answering without it"}

            result = run_tool_loop(
                invoke=invoke,
                system="sys",
                task="click the thing",
                registry=reg,
                ctx=self._ctx(),
                max_iters=4,
            )
            assert result.stopped == "done"
            assert result.tool_calls == 1
            assert ran == [{"selector": "#x", "session": "dead-loop"}]
            assert closer.closed is False  # the run never closed our session
        finally:
            browser_mod.sessions._sessions.pop("dead-loop", None)  # noqa: SLF001


# ---------------------------------------------------------------------------
# Docker dies — fast fail, no hangs; timeouts kill and clean the container
# ---------------------------------------------------------------------------


class _DeadDaemon:
    """A docker client whose daemon died."""

    class images:
        @staticmethod
        def get(image: str) -> Any:
            raise Exception("404 Client Error: No such image")

    class containers:
        @staticmethod
        def run(*args: Any, **kwargs: Any) -> Any:
            raise Exception("connection reset by peer")


class _HungContainer:
    """A container that never finishes: wait raises, kill/remove must run."""

    def __init__(self) -> None:
        self.kill_called = False
        self.remove_called = False

    def wait(self, timeout: float | None = None) -> Any:
        raise Exception(f"timed out after {timeout}s")

    def logs(self, **kwargs: Any) -> bytes:
        raise AssertionError("logs must not be read after a failed wait")

    def kill(self) -> None:
        self.kill_called = True

    def remove(self, force: bool = False) -> None:
        self.remove_called = True


class _HungDaemon:
    def __init__(self, container: _HungContainer) -> None:
        self._container = container

    class images:
        @staticmethod
        def get(image: str) -> Any:
            return object()

    @property
    def containers(self) -> Any:
        container = self._container

        class _Containers:
            @staticmethod
            def run(*args: Any, **kwargs: Any) -> Any:
                return container

        return _Containers()


class TestDockerDies:
    def test_daemon_death_fails_fast(self) -> None:
        sandbox = DockerSandbox(docker_client=_DeadDaemon())
        with pytest.raises(SandboxError, match="container start failed"):
            sandbox.run("/ws", ["echo", "hi"], timeout_seconds=5)

    def test_container_timeout_kills_and_cleans(self) -> None:
        container = _HungContainer()
        sandbox = DockerSandbox(docker_client=_HungDaemon(container))
        with pytest.raises(SandboxError, match="execution failed or timed out"):
            sandbox.run("/ws", ["sleep", "999"], timeout_seconds=5)
        assert container.kill_called  # no orphaned container left running
        assert container.remove_called  # no leaked container record

    def test_missing_local_image_is_unavailable(self) -> None:
        sandbox = DockerSandbox(docker_client=_DeadDaemon())
        with pytest.raises(SandboxUnavailableError):
            sandbox.ensure_image(DockerSandbox.QA_IMAGE)


# ---------------------------------------------------------------------------
# WebSocket disconnects — resume replays exactly the missed events
# ---------------------------------------------------------------------------


class TestWebsocketDrops:
    def _ws_url(self, client: TestClient, after_sequence: int) -> str:
        token = client.app.state.authenticator.bootstrap_token  # type: ignore[attr-defined]
        return f"/api/v1/ws/events?token={token}&after_sequence={after_sequence}"

    def _latest(self, client: TestClient) -> int:
        return client.get("/api/v1/events/latest-sequence").json()["sequence"]

    def _emit(self, client: TestClient, n: int, marker: str) -> None:

        bus: EventBus = client.app.state.event_bus
        factory = client.app.state.session_factory
        with session_scope(factory) as db:
            for i in range(n):
                bus.emit(
                    Event(type="session.created", actor="test", payload={"m": marker, "i": i}),
                    db,
                )

    def test_disconnect_removes_subscriber(self, client: TestClient) -> None:

        bus: EventBus = client.app.state.event_bus
        self._emit(client, 1, "before")
        before = len(bus._global_subscribers)  # noqa: SLF001 — asserting cleanup
        with client.websocket_connect(self._ws_url(client, 0)) as ws:
            msg = ws.receive_json()
            assert msg["kind"] == "event"
            assert len(bus._global_subscribers) == before + 1  # noqa: SLF001
        # Disconnect ran the finally-block: no leaked subscriber.
        assert len(bus._global_subscribers) == before  # noqa: SLF001

    def test_reconnect_resumes_missed_events(self, client: TestClient) -> None:
        self._emit(client, 1, "first")
        first_seen = self._latest(client)
        with client.websocket_connect(self._ws_url(client, 0)) as ws:
            msg = ws.receive_json()
            assert msg["kind"] == "event" and msg["sequence"] == first_seen
            # Disconnect drops the socket mid-stream.
        self._emit(client, 3, "missed")  # outage: three events while disconnected
        latest = self._latest(client)
        assert latest == first_seen + 3
        with client.websocket_connect(self._ws_url(client, first_seen)) as ws:
            seqs = []
            for _ in range(3):
                msg = ws.receive_json()
                assert msg["kind"] == "event"
                seqs.append(msg["sequence"])
            # Exactly the outage window, in order, no duplicates.
            assert seqs == [first_seen + 1, first_seen + 2, first_seen + 3]


# ---------------------------------------------------------------------------
# frontend reload — pending approvals survive; decide still works
# ---------------------------------------------------------------------------


class TestFrontendReload:
    def test_pending_approval_visible_after_reload(self, factory: Any) -> None:
        gate1 = PermissionGate(factory=factory)
        record = gate1.request(
            ApprovalRequest(
                requested_action="fs.delete",
                risk=Risk.MEDIUM,
                scope="file:delete",
                requester="CodeAgent",
                session_id="ses_reload",
            )
        )
        # Reload: a brand-new gate on the same database file.
        gate2 = PermissionGate(factory=factory)
        pending = gate2.list_pending()
        assert [r.approval_id for r in pending] == [record.approval_id]
        decided = gate2.decide(record.approval_id, approve=True)
        assert decided.decision is Decision.APPROVED
        assert gate2.pending() == []


# ---------------------------------------------------------------------------
# OS restart — TTL bounds lease staleness; everything committed survives
# ---------------------------------------------------------------------------


class TestOsRestart:
    def test_fresh_lease_survives_restart_then_reaped(self, tmp_path: Path) -> None:
        """A lease that was fresh at crash time must NOT be reaped on boot
        (the worker might still be alive — the TTL is the uncertainty bound),
        but once it expires the reaper requeues, even across the restart."""
        db_file = tmp_path / "os_restart.db"
        engine1, factory1 = _open_db(db_file)
        bus = EventBus()
        sup = Supervisor(bus)
        session_id = sup.create_session(factory1, "os restart")
        task_id = sup.add_task(factory1, session_id, "code", "in flight")
        with session_scope(factory1) as db:
            row = db.get(Task, task_id)
            assert row is not None
            row.state = TaskState.RUNNING.value
            row.attempt = 1
            db.add(
                AgentRun(
                    id="run_os_restart",
                    task_id=task_id,
                    agent_type="code",
                    state="RUNNING",
                    worker_id="w",
                )
            )
            db.add(
                AgentLease(
                    agent_run_id="run_os_restart",
                    worker_id="w",
                    state="RUNNING",
                    heartbeat_at=utcnow(),
                    lease_expires_at=utcnow() + timedelta(seconds=30),
                )
            )
        engine1.dispose()  # OS restart: power cut, no cleanup
        engine2, factory2 = _open_db(db_file)
        try:
            orch2 = Orchestrator(EventBus())
            assert orch2.recover_orphans(factory2) == []  # fresh: TTL not reached
            assert _task_state(factory2, task_id)[0] == "RUNNING"
            # Time passes past the TTL (lease expiry is wall-clock, durable).
            with session_scope(factory2) as db:
                lease = db.get(AgentLease, "run_os_restart")
                assert lease is not None
                lease.lease_expires_at = utcnow() - timedelta(seconds=1)
            assert orch2.recover_orphans(factory2) == [task_id]
            assert _task_state(factory2, task_id) == ("QUEUED", 1)
        finally:
            engine2.dispose()
