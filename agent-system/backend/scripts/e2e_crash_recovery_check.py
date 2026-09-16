"""E2E crash-recovery proof (Phase 4 acceptance): kill -9 the worker while a
task is RUNNING, then verify the lease reaper detects the stale lease and
recovers the task (RECOVERING -> QUEUED) without duplicate agent runs.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from agent_system.config import get_settings
from agent_system.domain import ids
from agent_system.domain.events import Event
from agent_system.domain.tasks import TaskState
from agent_system.infra.db import make_engine, make_session_factory, session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import AgentLease, AgentRun, Base, EventRow, Task
from agent_system.worker import enqueue_task

ORPHAN_SECONDS = 5.0  # lease expiry used for the reaper in this check


def main() -> int:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    bus = EventBus()

    session_id = ids.new_session_id()
    task_id = ids.new_task_id()
    with session_scope(factory) as db:
        from agent_system.infra.models import Session

        db.add(Session(id=session_id, goal="e2e: kill -9 crash recovery", status="ACTIVE"))
        db.add(
            Task(
                id=task_id,
                session_id=session_id,
                task_type="code",
                title="crash me mid-run",
                input_json={"simulate_seconds": 25},  # long enough to kill mid-run
                agent_type="code",
                state=TaskState.QUEUED.value,
            )
        )
        bus.emit(Event(type="task.queued", session_id=session_id, task_id=task_id, actor="e2e"), db)

    # Start a dedicated worker we can kill precisely.
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent_system.worker"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"worker pid: {proc.pid}")
    job_id = enqueue_task(factory, task_id=task_id)
    print(f"rq job: {job_id}")

    # Wait for RUNNING.
    deadline = time.time() + 30
    while time.time() < deadline:
        with session_scope(factory) as db:
            task = db.get(Task, task_id)
            if task and task.state == TaskState.RUNNING.value:
                break
        time.sleep(0.5)
    with session_scope(factory) as db:
        task = db.get(Task, task_id)
        if task is None or task.state != TaskState.RUNNING.value:
            print("task never reached RUNNING; aborting")
            proc.kill()
            return 1
        run = db.query(AgentRun).filter_by(task_id=task_id).first()
        print(f"task RUNNING, agent_run={run.id}")
        run_id = run.id

    # kill -9 mid-task
    proc.kill()
    proc.wait()
    print("worker killed with SIGKILL mid-task")

    # Expire the lease (shorten it for the test), then run the reaper.
    with session_scope(factory) as db:
        from agent_system.domain.events import utcnow

        lease = db.get(AgentLease, run_id)
        assert lease is not None, "lease must exist while task RUNNING"
        lease.lease_expires_at = utcnow()

    from agent_system.services.orchestrator import Orchestrator

    recovered = Orchestrator(EventBus()).recover_orphans(factory)
    print(f"reaper recovered: {recovered}")

    with session_scope(factory) as db:
        task = db.get(Task, task_id)
        run_count = db.query(AgentRun).filter_by(task_id=task_id).count()
        leases = db.query(AgentLease).filter_by(agent_run_id=run_id).count()
        events = [e.type for e in db.query(EventRow).filter_by(task_id=task_id).all()]
        print(f"task state: {task.state}, attempt: {task.attempt}")
        print(f"agent_runs for task: {run_count} (no duplicates)")
        print(f"stale leases left: {leases}")
        print(f"events: {sorted(set(events))}")

        ok = (
            task.state == TaskState.QUEUED.value
            and task.attempt == 1
            and run_count == 1
            and leases == 0
            and "recovery.started" in events
            and "recovery.completed" in events
        )
        print("\nCRASH RECOVERY: PASS" if ok else "\nCRASH RECOVERY: FAIL")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
