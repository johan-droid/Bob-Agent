"""E2E verification: RQ worker executes a task out-of-process (Phase 4).

Creates a real session + task in SQLite, walks it to QUEUED, enqueues the
task id on Redis (SQLite stays authoritative), then polls the DB until the
worker transitions it to SUCCEEDED with a real AgentRun + result.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from agent_system.config import get_settings
from agent_system.domain import ids
from agent_system.domain.events import Event
from agent_system.domain.tasks import TaskState
from agent_system.infra.db import make_engine, make_session_factory, session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import AgentLease, AgentRun, EventRow, Session, Task
from agent_system.worker import enqueue_task


def main() -> int:
    settings = get_settings()
    # CRITICAL: use the same SQLite file the worker resolves from settings —
    # SQLite is authoritative; the queue only carries task ids.
    db_path = Path(settings.database_url.replace("sqlite:///", ""))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = make_engine(settings.database_url)
    from agent_system.infra.models import Base

    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    bus = EventBus()

    # 1. Session
    session_id = ids.new_session_id()
    with session_scope(factory) as db:
        db.add(Session(id=session_id, goal="e2e: verify RQ worker executes tasks", status="ACTIVE"))
        bus.emit(Event(type="session.created", session_id=session_id, actor="e2e"), db)
    print(f"session:  {session_id}")

    # 2. Task (PENDING -> QUEUED with a deterministic handler type)
    task_id = ids.new_task_id()
    with session_scope(factory) as db:
        db.add(
            Task(
                id=task_id,
                session_id=session_id,
                task_type="code",
                title="e2e worker execution",
                input_json={"simulate_seconds": 0.2, "marker": "e2e-worker-check"},
                agent_type="code",
                state=TaskState.PENDING.value,
            )
        )
        bus.emit(
            Event(type="task.created", session_id=session_id, task_id=task_id, actor="e2e"), db
        )
        validate_ok = True
    with session_scope(factory) as db:
        task = db.get(Task, task_id)
        assert task is not None
        task.state = TaskState.QUEUED.value
        bus.emit(Event(type="task.queued", session_id=session_id, task_id=task_id, actor="e2e"), db)
    print(f"task:     {task_id} (QUEUED)")

    # 3. Enqueue on Redis — SQLite remains the source of truth
    job_id = enqueue_task(factory, task_id=task_id)
    print(f"rq job:   {job_id} (enqueued on redis)")

    # 4. Poll for out-of-process completion
    deadline = time.time() + 60
    final_state = None
    while time.time() < deadline:
        with session_scope(factory) as db:
            task = db.get(Task, task_id)
            final_state = task.state if task else None
            if final_state in (TaskState.SUCCEEDED.value, TaskState.FAILED.value):
                break
        time.sleep(1)

    # 5. Verify durable state
    with session_scope(factory) as db:
        task = db.get(Task, task_id)
        run = db.query(AgentRun).filter_by(task_id=task_id).first()
        lease = db.query(AgentLease).filter_by(agent_run_id=run.id).first() if run else None
        events = (
            db.query(EventRow).filter(EventRow.task_id == task_id).order_by(EventRow.sequence).all()
        )
        event_types = [e.type for e in events]

        print("\n--- results ---")
        print(f"final task state: {task.state}")
        print(f"task attempt:     {task.attempt}")
        print(f"result:           {task.result_json}")
        worker_id = run.worker_id if run else None
        run_state = run.state if run else None
        print(f"agent run:        {run.id if run else None} state={run_state} worker={worker_id}")
        print(f"lease remaining:  {'yes (BUG)' if lease else 'cleaned up'}")
        print(f"task events:      {event_types}")

        ok = (
            task.state == TaskState.SUCCEEDED.value
            and run is not None
            and run.state == "COMPLETED"
            and run.worker_id not in (None, "inproc")
            and lease is None
            and "task.started" in event_types
            and "task.completed" in event_types
            and validate_ok
        )
        print(
            f"\nworker_id '{worker_id}' proves out-of-process execution" if ok else "\nE2E FAILED"
        )
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
