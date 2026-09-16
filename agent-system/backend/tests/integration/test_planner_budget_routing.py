"""Integration — planning, restart-safe budgets, routing, context retention.

Covers the DoD items that are otherwise only assertions in prose:

- Planner produces a DAG; the Supervisor validates and persists it; the
  Orchestrator executes it in dependency order (v3.1 §7).
- An invalid plan fails closed instead of executing (cycles, unknown deps,
  unavailable capabilities).
- Budget accounting is derived from persisted model calls, so a *new process*
  sees the same spend and the same exhausted budget (v3.1 §12).
- Unknown agent types do not silently become generic LLM execution, and a
  fallback that does run is recorded as an event (v3.1 §3/§4).
- Compaction keeps important results and drops low-value raw output (v3.1 §11).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_system.agents import registry
from agent_system.agents.definitions import definition_for
from agent_system.agents.registry import UnknownAgentTypeError, resolve
from agent_system.domain.tasks import TaskState
from agent_system.infra.db import make_engine, make_session_factory, session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Base, EventRow, ModelCall, Task
from agent_system.services.budget import BudgetLedger
from agent_system.services.context import ContextManager, classify_importance
from agent_system.services.orchestrator import Supervisor
from agent_system.services.planner import Planner, PlanningError


@pytest.fixture()
def factory(tmp_path: Path) -> Any:
    engine = make_engine(f"sqlite:///{tmp_path / 'plan.db'}")
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


class TestPlanner:
    def test_code_goal_classifies_intent_and_capabilities(self) -> None:
        plan = Planner().plan("Refactor the auth module and run the tests")
        assert plan.intent == "code"
        assert "edit_source" in plan.required_capabilities
        assert plan.strategy.startswith("deterministic")
        assert plan.tasks

    def test_document_goal_builds_a_two_node_dag(self) -> None:
        plan = Planner().plan("Write a PDF report about quarterly revenue")
        assert plan.intent == "documents"
        assert len(plan.tasks) == 2
        gather, produce = plan.tasks
        assert produce.depends_on == (gather.key,)

    def test_high_risk_keywords_escalate(self) -> None:
        assert Planner().plan("Deploy the new build to production").risk == "HIGH"

    def test_empty_goal_is_rejected(self) -> None:
        with pytest.raises(PlanningError):
            Planner().plan("   ")

    def test_plan_serialises_for_persistence(self) -> None:
        payload = Planner().plan("Research the topic").to_json()
        assert payload["intent"] == "research"
        assert isinstance(payload["tasks"], list)


class TestSupervisorPlanValidation:
    def test_cycle_is_rejected(self, factory: Any) -> None:
        from agent_system.services.planner import PlannedTask, TaskPlan

        plan = TaskPlan(
            goal="g",
            intent="generic",
            risk="LOW",
            tasks=(
                PlannedTask(
                    key="a", title="a", task_type="llm", agent_type="llm", depends_on=("b",)
                ),
                PlannedTask(
                    key="b", title="b", task_type="llm", agent_type="llm", depends_on=("a",)
                ),
            ),
        )
        with pytest.raises(ValueError, match="cycle"):
            Supervisor(EventBus()).validate_plan(plan)

    def test_unknown_dependency_is_rejected(self) -> None:
        from agent_system.services.planner import PlannedTask, TaskPlan

        plan = TaskPlan(
            goal="g",
            intent="generic",
            risk="LOW",
            tasks=(
                PlannedTask(
                    key="a", title="a", task_type="llm", agent_type="llm", depends_on=("ghost",)
                ),
            ),
        )
        with pytest.raises(ValueError, match="unknown task"):
            Supervisor(EventBus()).validate_plan(plan)

    def test_unavailable_capability_fails_closed(self) -> None:
        plan = Planner().plan("Fix the code in the repo")
        with pytest.raises(ValueError, match="unavailable capabilities"):
            Supervisor(EventBus()).validate_plan(plan, available_capabilities={"file_read"})

    def test_plan_is_persisted_in_dependency_order(self, factory: Any) -> None:
        bus = EventBus()
        supervisor = Supervisor(bus)
        session_id = supervisor.create_session(factory, "Write a PDF report on Q3")
        plan = Planner().plan("Write a PDF report on Q3")
        ids = supervisor.apply_plan(factory, session_id, plan)
        assert len(ids) == len(plan.tasks)
        with session_scope(factory) as db:
            rows = {row.id: row for row in db.query(Task).filter_by(session_id=session_id)}
            produce = rows[ids[1]]
            gather = rows[ids[0]]
            # The dependent task points at the *created* id of its dependency.
            assert produce.depends_on_json == [gather.id]
        assert supervisor.plan(factory, session_id) == [ids[0]]

    def test_high_risk_plan_warns(self) -> None:
        plan = Planner().plan("Deploy to production and delete the old release")
        warnings = Supervisor(EventBus()).validate_plan(plan)
        assert any("HIGH" in warning for warning in warnings)


class TestBudgetSurvivesRestart:
    def _record(
        self, factory: Any, *, cost: float, session_id: str = "ses_1", task_id: str = "task_1"
    ) -> None:
        with session_scope(factory) as db:
            db.add(
                ModelCall(
                    id=f"mc_{cost}_{session_id}_{task_id}_{db.query(ModelCall).count()}",
                    task_id=task_id,
                    session_id=session_id,
                    provider="openai",
                    model_id="gpt-x",
                    status="ok",
                    tokens_in=1000,
                    tokens_out=500,
                    cost_usd=cost,
                )
            )

    def test_spend_is_derived_from_persisted_records(self, factory: Any) -> None:
        self._record(factory, cost=1.5)
        self._record(factory, cost=0.5)
        ledger = BudgetLedger(factory, SimpleNamespace(daily_budget_usd=10.0))
        assert ledger.daily_usage().spent_usd == pytest.approx(2.0)
        assert ledger.task_usage("task_1").spent_usd == pytest.approx(2.0)
        assert ledger.session_usage("ses_1").spent_usd == pytest.approx(2.0)
        assert ledger.provider_usage("openai").spent_usd == pytest.approx(2.0)

    def test_a_new_process_sees_the_same_spend(self, factory: Any) -> None:
        self._record(factory, cost=3.0)
        # Simulate a restart: a brand-new ledger over the same database.
        fresh = BudgetLedger(factory, SimpleNamespace(daily_budget_usd=10.0))
        assert fresh.daily_usage().spent_usd == pytest.approx(3.0)

    def test_exhausted_daily_budget_blocks_after_restart(self, factory: Any) -> None:
        self._record(factory, cost=9.99)
        restarted = BudgetLedger(factory, SimpleNamespace(daily_budget_usd=10.0))
        assert restarted.check().allowed
        self._record(factory, cost=0.02)
        stricter = BudgetLedger(factory, SimpleNamespace(daily_budget_usd=10.0))
        decision = stricter.check()
        assert not decision.allowed
        assert decision.scope == "daily"
        assert "daily budget exhausted" in str(decision.reason)

    def test_task_token_limit_is_enforced(self, factory: Any) -> None:
        self._record(factory, cost=0.01)
        ledger = BudgetLedger(factory, SimpleNamespace(max_task_tokens=100, max_task_cost_usd=99))
        decision = ledger.check(task_id="task_1")
        assert not decision.allowed
        assert decision.scope == "task"

    def test_task_cost_limit_is_enforced(self, factory: Any) -> None:
        self._record(factory, cost=5.0)
        ledger = BudgetLedger(
            factory, SimpleNamespace(max_task_cost_usd=2.0, max_task_tokens=10**9)
        )
        assert not ledger.check(task_id="task_1").allowed


class TestAgentRoutingIsExplicit:
    def test_unknown_type_without_fallback_raises(self) -> None:
        snapshot = registry.snapshot()
        registry.set_fallback(None)
        try:
            with pytest.raises(UnknownAgentTypeError):
                resolve("no-such-agent")
        finally:
            registry.restore(snapshot)

    def test_fallback_requires_explicit_installation(self) -> None:
        snapshot = registry.snapshot()
        try:
            registry.set_fallback(None)
            assert not registry.fallback_installed()
            registry.set_fallback(
                lambda task, context: {"ok": True}, reason="unsupported_agent_type"
            )
            assert registry.fallback_installed()
            resolution = resolve("no-such-agent")
            assert resolution.is_fallback
            assert resolution.fallback_reason == "unsupported_agent_type"
            assert resolution.definition.name == "generic"
        finally:
            registry.restore(snapshot)

    def test_registered_type_never_reports_fallback(self) -> None:
        snapshot = registry.snapshot()
        try:
            registry.register("code", lambda task, context: {"ok": True})
            resolution = resolve("code")
            assert not resolution.is_fallback
            assert resolution.definition.name == "code"
        finally:
            registry.restore(snapshot)

    def test_fallback_decision_is_recorded_as_an_event(self, factory: Any) -> None:
        snapshot = registry.snapshot()
        bus = EventBus()
        try:
            registry.set_fallback(lambda task, context: {"ok": True})
            registry.run_agent(
                "unregistered_type",
                {"goal": "do something"},
                {
                    "session_id": "ses_r",
                    "task_id": "task_r",
                    "agent_run_id": "run_r",
                    "factory": factory,
                    "bus": bus,
                    "agent_type": "unregistered_type",
                },
            )
        finally:
            registry.restore(snapshot)
        with session_scope(factory) as db:
            events = db.query(EventRow).filter_by(type="agent.fallback_applied").all()
        assert events, "fallback routing must be recorded, never silent"
        assert events[0].payload["requested_type"] == "unregistered_type"
        assert events[0].payload["fallback_reason"] == "unsupported_agent_type"

    def test_declared_definitions_cover_the_named_agents(self) -> None:
        for name in ("code", "research", "browser", "documents", "qa", "scheduler", "generic"):
            assert definition_for(name) is not None, name

    def test_run_agent_reports_its_routing(self) -> None:
        snapshot = registry.snapshot()
        try:
            registry.register("code", lambda task, context: {"did": "work"})
            result = registry.run_agent("code", {}, {})
            assert result["_routing"]["resolved_agent"] == "code"
            assert result["_routing"]["fallback_reason"] is None
        finally:
            registry.restore(snapshot)


class TestContextRetention:
    def test_errors_are_important_and_listings_are_not(self) -> None:
        assert classify_importance("shell", {"error": "boom"}) > classify_importance(
            "file_list", {"entries": ["a", "b"]}
        )
        assert classify_importance("run_tests", {"exit_code": 1}) > classify_importance(
            "file_read", {"content": "x" * 5000}
        )

    def test_compaction_keeps_important_results_and_drops_bulk(self) -> None:
        manager = ContextManager(
            max_context_tokens=1000, compaction_threshold_pct=50.0, keep_recent=1
        )
        error_block = '<tool_result name="shell">{"error": "pytest failed"}</tool_result>'
        bulk_block = '<tool_result name="file_read">' + ("x" * 3000) + "</tool_result>"
        manager.observe(error_block, name="shell", result={"error": "pytest failed"})
        manager.observe(bulk_block, name="file_read", result={"content": "x" * 3000})
        manager.observe(
            '<tool_result name="file_list">{"entries": []}</tool_result>',
            name="file_list",
            result={"entries": []},
        )
        assert manager.should_compact()
        result = manager.compact(transcript=error_block + bulk_block)
        assert result is not None
        assert "file_read" in result.dropped_names
        assert "shell" not in result.dropped_names
        assert "important" in result.transcript.lower()

    def test_no_compaction_under_budget(self) -> None:
        manager = ContextManager(max_context_tokens=100_000, compaction_threshold_pct=75.0)
        manager.observe(
            '<tool_result name="file_read">{"content": "small"}</tool_result>', name="file_read"
        )
        assert manager.compact(transcript="x") is None


class TestEndToEndExecutionPath:
    """goal -> plan -> tasks -> orchestrator -> model -> events -> persistence."""

    def test_planned_session_executes_in_dependency_order(self, factory: Any) -> None:
        from agent_system.services.orchestrator import Orchestrator

        bus = EventBus()
        supervisor = Supervisor(bus)
        executed: list[str] = []

        def handler(task_input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
            executed.append(str(context.get("task_id")))
            return {"output": f"did {task_input.get('goal', '')[:20]}"}

        orchestrator = Orchestrator(bus)
        orchestrator.register_handler("llm", handler)
        session_id = supervisor.create_session(factory, "Write a PDF report on Q3")
        plan = Planner().plan("Write a PDF report on Q3")
        task_ids = supervisor.apply_plan(factory, session_id, plan)

        # First pass runs only the dependency-free task.
        started = orchestrator.run_ready_tasks(factory, session_id)
        assert started == [task_ids[0]]
        # Second pass runs the dependent task once its dependency succeeded.
        started = orchestrator.run_ready_tasks(factory, session_id)
        assert started == [task_ids[1]]
        assert executed == task_ids

        with session_scope(factory) as db:
            rows = db.query(Task).filter_by(session_id=session_id).all()
            assert all(row.state == TaskState.SUCCEEDED.value for row in rows)
            types = [row.type for row in db.query(EventRow).order_by(EventRow.sequence).all()]
        for expected in (
            "session.created",
            "task.created",
            "task.queued",
            "task.started",
            "task.completed",
        ):
            assert expected in types
