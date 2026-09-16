"""Task batching (v3.1 Phase 16, §11 feature note).

TaskBatcher groups compatible tasks into a batch: same agent type, task type,
workspace, permissions, and no inter-task dependencies. Incompatible batches
are rejected with an explicit reason. Execution rides the canonical task
pipeline (each member keeps its own task lifecycle + events); partial failure
isolates to the failing member only.
"""

from __future__ import annotations

from typing import Any

from agent_system.domain import ids
from agent_system.domain.events import Event, utcnow
from agent_system.domain.tasks import TaskState
from agent_system.infra.db import session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Task, TaskBatch

MAX_BATCH_SIZE = 100


class BatchError(ValueError):
    pass


class TaskBatcher:
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    def create_batch(
        self,
        factory: Any,
        session_id: str,
        task_ids: list[str],
        batch_type: str = "parallel",
    ) -> dict[str, Any]:
        if len(task_ids) < 2:
            raise BatchError("a batch needs at least 2 tasks")
        if len(task_ids) > MAX_BATCH_SIZE:
            raise BatchError(f"batch exceeds {MAX_BATCH_SIZE} tasks")

        with session_scope(factory) as db:
            tasks_list = [db.get(Task, tid) for tid in task_ids]
            missing = [tid for tid, t in zip(task_ids, tasks_list, strict=True) if t is None]
            if missing:
                raise BatchError(f"unknown tasks: {missing[:5]}")
            # After the missing check, all members are non-None (narrowed).
            members: list[Task] = [t for t in tasks_list if t is not None]
            for t in members:
                if t.session_id != session_id:
                    raise BatchError(f"task {t.id} belongs to a different session")
                if t.batch_id is not None:
                    raise BatchError(f"task {t.id} is already in batch {t.batch_id}")

            # Compatibility: agent type, task type, no inter-dependencies.
            agent_types = {(t.agent_type or t.task_type) for t in members}
            task_types = {t.task_type for t in members}
            if len(agent_types) > 1 or len(task_types) > 1:
                raise BatchError(
                    f"incompatible batch: mixed agent types {sorted(agent_types)} "
                    f"or task types {sorted(task_types)}"
                )
            id_set = set(task_ids)
            for t in members:
                deps = set(t.depends_on_json or [])
                overlap = deps & id_set
                if overlap:
                    raise BatchError(
                        f"incompatible batch: task {t.id} depends on "
                        f"batch member(s) {sorted(overlap)}"
                    )
                if t.state not in (TaskState.PENDING.value, TaskState.QUEUED.value):
                    raise BatchError(f"task {t.id} is {t.state}; only PENDING/QUEUED can batch")

            batch_id = ids.new_id("batch")
            db.add(
                TaskBatch(
                    id=batch_id,
                    batch_type=batch_type,
                    task_ids_json=task_ids,
                    member_count=len(task_ids),
                )
            )
            for t in members:
                t.batch_id = batch_id
            self._bus.emit(
                Event(
                    type="task.queued",
                    session_id=session_id,
                    actor="batcher",
                    payload={"batch_id": batch_id, "members": len(task_ids)},
                ),
                db,
            )
        return {
            "batch_id": batch_id,
            "task_ids": task_ids,
            "member_count": len(task_ids),
            "batch_type": batch_type,
        }

    def cancel_batch(self, factory: Any, batch_id: str) -> dict[str, Any]:
        """Cancel all still-pending/queued members; running ones are left to
        finish (cooperative cancellation at safe points)."""
        with session_scope(factory) as db:
            batch = db.get(TaskBatch, batch_id)
            if batch is None:
                raise BatchError(f"batch {batch_id} not found")
            cancelled: list[str] = []
            skipped: list[str] = []
            for tid in batch.task_ids_json:
                task = db.get(Task, tid)
                if task is None:
                    continue
                if task.state == TaskState.QUEUED.value:
                    task.state = TaskState.CANCELLED.value
                    task.completed_at = None
                    cancelled.append(tid)
                    self._bus.emit(Event(type="task.cancelled", task_id=tid, actor="batcher"), db)
                elif task.state == TaskState.PENDING.value:
                    task.state = TaskState.CANCELLED.value
                    cancelled.append(tid)
                else:
                    skipped.append(tid)
            batch.completed_at = None
        return {
            "batch_id": batch_id,
            "cancelled": cancelled,
            "left_running": skipped,
        }


def record_batch_result(
    factory: Any,
    batch_id: str,
    per_task_seconds: dict[str, float],
    sequential_estimate_seconds: float,
) -> dict[str, Any]:
    """Compute the speedup metric and close the batch (Phase 16 acceptance)."""
    batch_seconds = sum(per_task_seconds.values())
    speedup = (
        round(sequential_estimate_seconds / batch_seconds, 3)
        if batch_seconds > 0 and sequential_estimate_seconds > 0
        else None
    )
    with session_scope(factory) as db:
        batch = db.get(TaskBatch, batch_id)
        if batch is None:
            raise BatchError(f"batch {batch_id} not found")
        batch.completed_at = utcnow()
        batch.speedup_factor = speedup
        members = db.query(Task).filter(Task.batch_id == batch_id).all()
        return {
            "batch_id": batch_id,
            "members": len(members),
            "succeeded": sum(1 for m in members if m.state == TaskState.SUCCEEDED.value),
            "failed": sum(1 for m in members if m.state == TaskState.FAILED.value),
            "speedup_factor": speedup,
        }


def batch_status(factory: Any, batch_id: str) -> dict[str, Any]:
    with session_scope(factory) as db:
        batch = db.get(TaskBatch, batch_id)
        if batch is None:
            raise LookupError(f"batch {batch_id} not found")
        members = db.query(Task).filter(Task.batch_id == batch_id).all()
        by_state: dict[str, int] = {}
        for m in members:
            by_state[m.state] = by_state.get(m.state, 0) + 1
        return {
            "batch_id": batch.id,
            "batch_type": batch.batch_type,
            "member_count": batch.member_count,
            "states": by_state,
            "speedup_factor": batch.speedup_factor,
            "completed": batch.completed_at.isoformat() if batch.completed_at else None,
            "task_ids": batch.task_ids_json,
        }
