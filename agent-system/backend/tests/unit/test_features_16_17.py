"""Phase 15–17 — unit tests for batching, recipes, personality, insights."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_system.domain.events import Event
from agent_system.infra.db import make_engine, make_session_factory, session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Base, Task
from agent_system.services.batching import BatchError, TaskBatcher, batch_status
from agent_system.services.insights import InsightGenerator
from agent_system.services.personality import PersonalityError, PersonalityManager
from agent_system.services.recipes import RecipeEngine, RecipeError, _validate_dag


@pytest.fixture()
def factory(tmp_path: Path) -> Any:
    engine = make_engine(f"sqlite:///{tmp_path / 'feat.db'}")
    Base.metadata.create_all(engine)
    f = make_session_factory(engine)
    yield f
    engine.dispose()


def _make_task(
    factory: Any,
    bus: EventBus,
    session_id: str,
    task_type: str = "code",
    agent_type: str | None = None,
    depends_on: list[str] | None = None,
    state: str = "QUEUED",
) -> str:
    from agent_system.domain import ids

    task_id = ids.new_task_id()
    with session_scope(factory) as db:
        db.add(
            Task(
                id=task_id,
                session_id=session_id,
                task_type=task_type,
                title=f"t-{task_id[-6:]}",
                agent_type=agent_type or task_type,
                depends_on_json=depends_on or [],
                state=state,
            )
        )
        bus.emit(
            Event(type="task.created", session_id=session_id, task_id=task_id, actor="test"),
            db,
        )
    return task_id


def _make_session(factory: Any, bus: EventBus) -> str:
    from agent_system.domain import ids
    from agent_system.infra.models import Session

    session_id = ids.new_session_id()
    with session_scope(factory) as db:
        db.add(Session(id=session_id, goal="batch test", status="ACTIVE"))
    return session_id


class TestBatching:
    def test_create_compatible_batch(self, factory: Any) -> None:
        bus = EventBus()
        session_id = _make_session(factory, bus)
        t1 = _make_task(factory, bus, session_id)
        t2 = _make_task(factory, bus, session_id)
        result = TaskBatcher(bus).create_batch(factory, session_id, [t1, t2])
        assert result["member_count"] == 2
        assert result["batch_id"].startswith("batch_")
        status = batch_status(factory, result["batch_id"])
        assert status["member_count"] == 2

    def test_mixed_agent_types_rejected(self, factory: Any) -> None:
        bus = EventBus()
        session_id = _make_session(factory, bus)
        t1 = _make_task(factory, bus, session_id, task_type="code")
        t2 = _make_task(factory, bus, session_id, task_type="research")
        with pytest.raises(BatchError, match="incompatible"):
            TaskBatcher(bus).create_batch(factory, session_id, [t1, t2])

    def test_inter_dependencies_rejected(self, factory: Any) -> None:
        bus = EventBus()
        session_id = _make_session(factory, bus)
        t1 = _make_task(factory, bus, session_id)
        t2 = _make_task(factory, bus, session_id, depends_on=[t1])
        with pytest.raises(BatchError, match="depends on batch member"):
            TaskBatcher(bus).create_batch(factory, session_id, [t1, t2])

    def test_single_task_rejected(self, factory: Any) -> None:
        bus = EventBus()
        session_id = _make_session(factory, bus)
        t1 = _make_task(factory, bus, session_id)
        with pytest.raises(BatchError, match="at least 2"):
            TaskBatcher(bus).create_batch(factory, session_id, [t1])

    def test_unknown_task_rejected(self, factory: Any) -> None:
        bus = EventBus()
        session_id = _make_session(factory, bus)
        t1 = _make_task(factory, bus, session_id)
        with pytest.raises(BatchError, match="unknown tasks"):
            TaskBatcher(bus).create_batch(factory, session_id, [t1, "task_missing"])

    def test_cancel_batch_cancels_queued_only(self, factory: Any) -> None:
        bus = EventBus()
        session_id = _make_session(factory, bus)
        t1 = _make_task(factory, bus, session_id, state="QUEUED")
        t2 = _make_task(factory, bus, session_id, state="QUEUED")
        batch = TaskBatcher(bus).create_batch(factory, session_id, [t1, t2])
        # One member started running before cancellation — it is left alone.
        with session_scope(factory) as db:
            db.get(Task, t2).state = "RUNNING"
        result = TaskBatcher(bus).cancel_batch(factory, batch["batch_id"])
        assert result["cancelled"] == [t1]
        assert result["left_running"] == [t2]

    def test_partial_failure_isolation(self, factory: Any) -> None:
        """One member failing never blocks the others (states stay independent)."""
        bus = EventBus()
        session_id = _make_session(factory, bus)
        t1 = _make_task(factory, bus, session_id)
        t2 = _make_task(factory, bus, session_id)
        batch = TaskBatcher(bus).create_batch(factory, session_id, [t1, t2])
        with session_scope(factory) as db:
            db.get(Task, t1).state = "FAILED"
            db.get(Task, t2).state = "SUCCEEDED"
        status = batch_status(factory, batch["batch_id"])
        assert status["states"] == {"FAILED": 1, "SUCCEEDED": 1}


VALID_DAG = {
    "tasks": [
        {"key": "a", "task_type": "code", "title": "step {{name}}"},
        {"key": "b", "task_type": "code", "title": "finish", "depends_on": ["a"]},
    ]
}


class TestRecipes:
    def test_create_and_get(self, factory: Any) -> None:
        engine = RecipeEngine(EventBus())
        created = engine.create_recipe(factory, "deploy", VALID_DAG, {"name": "x"})
        assert created["steps"] == 2
        got = engine.get_recipe(factory, created["recipe_id"])
        assert got is not None and got["name"] == "deploy"

    def test_invalid_dag_rejected(self, factory: Any) -> None:
        with pytest.raises(RecipeError, match="tasks"):
            _validate_dag({})
        with pytest.raises(RecipeError, match="cycle"):
            _validate_dag(
                {
                    "tasks": [
                        {"key": "a", "task_type": "c", "title": "x", "depends_on": ["b"]},
                        {"key": "b", "task_type": "c", "title": "y", "depends_on": ["a"]},
                    ]
                }
            )
        with pytest.raises(RecipeError, match="unknown"):
            _validate_dag(
                {"tasks": [{"key": "a", "task_type": "c", "title": "x", "depends_on": ["zz"]}]}
            )

    def test_execute_creates_real_tasks(self, factory: Any) -> None:
        bus = EventBus()
        engine = RecipeEngine(bus)
        created = engine.create_recipe(factory, "run", VALID_DAG, {"name": "alpha"})
        result = engine.execute(factory, created["recipe_id"])
        assert result["session_id"].startswith("ses_")
        assert len(result["task_ids"]) == 2
        with session_scope(factory) as db:
            task_a = db.get(Task, result["task_ids"][0])
            task_b = db.get(Task, result["task_ids"][1])
            assert task_a is not None and task_b is not None
            assert task_a.title == "step alpha"  # parameter substituted
            # dependency resolved to the real task id of step a
            assert task_b.depends_on_json == [result["task_ids"][0]]

    def test_execute_missing_param_rejected(self, factory: Any) -> None:
        engine = RecipeEngine(EventBus())
        created = engine.create_recipe(factory, "run", VALID_DAG)  # no params
        with pytest.raises(RecipeError, match="missing recipe parameters"):
            engine.execute(factory, created["recipe_id"])


class TestPersonality:
    def test_update_bumps_version(self, factory: Any) -> None:
        mgr = PersonalityManager(factory)
        first = mgr.update("code", {"tone": "casual", "verbosity": 3})
        assert first["version"] == 1
        second = mgr.update("code", {"tone": "formal"})
        assert second["version"] == 2
        assert second["verbosity"] == 3

    def test_security_fields_rejected(self, factory: Any) -> None:
        mgr = PersonalityManager(factory)
        with pytest.raises(PersonalityError, match="not personality-adjustable"):
            mgr.update("code", {"permission_policy": "ALLOW_ALL"})
        with pytest.raises(PersonalityError, match="not personality-adjustable"):
            mgr.update("code", {"max_cost_usd": 9999})

    def test_learning_requires_threshold(self, factory: Any) -> None:
        mgr = PersonalityManager(factory)
        for _ in range(5):
            mgr.record_feedback("code", 1, None, None)
        result = mgr.learn_from_feedback("code")
        assert result["learned"] is False

    def test_learning_adjusts_prompt_fields_only(self, factory: Any) -> None:
        mgr = PersonalityManager(factory)
        mgr.update("code", {"verbosity": 7})
        for _ in range(10):
            mgr.record_feedback("code", 1, "too verbose", None)
        result = mgr.learn_from_feedback("code")
        assert result["learned"] is True
        after = mgr.get("code")
        assert after is not None
        assert after["verbosity"] == 5  # 7 - 2
        # no security-ish fields appeared
        assert "permission_policy" not in after


class TestInsights:
    def test_daily_insight_from_events(self, factory: Any) -> None:
        bus = EventBus()
        session_id = _make_session(factory, bus)
        for _ in range(3):
            tid = _make_task(factory, bus, session_id)
            with session_scope(factory) as db:
                bus.emit(Event(type="task.completed", task_id=tid, actor="test"), db)
        gen = InsightGenerator(factory, bus)
        result = gen.generate("daily")
        assert result["insight_id"].startswith("insight_")
        assert any("events analyzed" in f["finding"] for f in result["findings"])
        assert "3 tasks completed" in result["content"] or "3" in result["content"]

    def test_empty_history_honest(self, factory: Any) -> None:
        gen = InsightGenerator(factory, EventBus())
        result = gen.generate("daily")
        assert result["findings"] == []
        assert "No events" in result["content"]

    def test_anomaly_detects_high_failure_rate(self, factory: Any) -> None:
        bus = EventBus()
        session_id = _make_session(factory, bus)
        for i in range(8):
            tid = _make_task(factory, bus, session_id)
            with session_scope(factory) as db:
                kind = "task.failed" if i < 6 else "task.completed"
                bus.emit(Event(type=kind, task_id=tid, actor="test"), db)
        result = InsightGenerator(factory, bus).generate("anomaly")
        kinds = [f["kind"] for f in result["findings"]]
        assert "failure_rate" in kinds
        rate_finding = next(f for f in result["findings"] if f["kind"] == "failure_rate")
        assert "suggested_action" in rate_finding

    def test_insights_never_fabricate(self, factory: Any) -> None:
        """With no events, the daily insight must not invent numbers."""
        bus = EventBus()
        result = InsightGenerator(factory, bus).generate("weekly")
        assert "0 tasks completed, 0 failed" not in result["content"]
        assert result["findings"] == []
