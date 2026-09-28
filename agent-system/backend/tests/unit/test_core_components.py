"""Tests for Jev decision engine, rollback, hooks, auto mode, verification loop."""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent_system.services.jev import (
    Environment,
    Jev,
    JevDecision,
    ToolCategory,
    get_jev,
    reset_jev,
)
from agent_system.services.permissions import AutonomyMode

# ---------------------------------------------------------------------------
# Jev tests
# ---------------------------------------------------------------------------


class TestJevDecisionEngine:
    """Jev deterministic decision engine tests."""

    def setup_method(self) -> None:
        reset_jev()

    def test_low_risk_allows_in_build_mode(self) -> None:
        jev = Jev(environment=Environment.DEVELOPMENT, autonomy_mode=AutonomyMode.BUILD)
        verdict = jev.evaluate_tool_call(
            tool_name="read_file",
            tool_category=ToolCategory.FILESYSTEM,
            is_destructive=False,
        )
        assert verdict.decision is JevDecision.ALLOW

    def test_critical_risk_always_asks(self) -> None:
        jev = Jev(environment=Environment.DEVELOPMENT, autonomy_mode=AutonomyMode.AUTO)
        verdict = jev.evaluate_tool_call(
            tool_name="rm_rf",
            tool_category=ToolCategory.SHELL,
            is_destructive=True,
        )
        assert verdict.decision is JevDecision.ASK

    def test_dangerous_scope_denies(self) -> None:
        jev = Jev(environment=Environment.DEVELOPMENT, autonomy_mode=AutonomyMode.UNRESTRICTED)
        verdict = jev.evaluate_tool_call(
            tool_name="read_file",
            tool_category=ToolCategory.FILESYSTEM,
            scope="host:credentials",
        )
        assert verdict.decision is JevDecision.DENY

    def test_plan_mode_denies_non_low_risk(self) -> None:
        jev = Jev(environment=Environment.DEVELOPMENT, autonomy_mode=AutonomyMode.PLAN)
        verdict = jev.evaluate_tool_call(
            tool_name="write_file",
            tool_category=ToolCategory.FILESYSTEM,
        )
        assert verdict.decision is JevDecision.DENY

    def test_auto_mode_allows_medium_risk(self) -> None:
        jev = Jev(environment=Environment.DEVELOPMENT, autonomy_mode=AutonomyMode.AUTO)
        verdict = jev.evaluate_tool_call(
            tool_name="run_tests",
            tool_category=ToolCategory.SHELL,
        )
        assert verdict.decision is JevDecision.ALLOW

    def test_high_risk_asks_in_build_mode(self) -> None:
        jev = Jev(environment=Environment.DEVELOPMENT, autonomy_mode=AutonomyMode.BUILD)
        verdict = jev.evaluate_tool_call(
            tool_name="git_push",
            tool_category=ToolCategory.GIT,
            is_remote=True,
        )
        assert verdict.decision is JevDecision.ASK

    def test_admin_override_in_unrestricted_mode(self) -> None:
        jev = Jev(environment=Environment.DEVELOPMENT, autonomy_mode=AutonomyMode.UNRESTRICTED)
        verdict = jev.evaluate_tool_call(
            tool_name="git_push",
            tool_category=ToolCategory.GIT,
            user_role="admin",
            is_remote=True,
        )
        assert verdict.decision is JevDecision.ALLOW

    def test_production_escalates_medium_risk(self) -> None:
        jev = Jev(environment=Environment.PRODUCTION, autonomy_mode=AutonomyMode.BUILD)
        verdict = jev.evaluate_tool_call(
            tool_name="modify_file",
            tool_category=ToolCategory.FILESYSTEM,
        )
        assert verdict.decision is JevDecision.ASK

    def test_repeated_failures_escalate(self) -> None:
        jev = Jev(environment=Environment.DEVELOPMENT, autonomy_mode=AutonomyMode.BUILD)
        verdict = jev.evaluate_tool_call(
            tool_name="read_file",
            tool_category=ToolCategory.FILESYSTEM,
            previous_failures=3,
        )
        assert verdict.decision is JevDecision.ASK

    def test_strict_mode_escalates_low_risk(self) -> None:
        jev = Jev(
            environment=Environment.DEVELOPMENT,
            autonomy_mode=AutonomyMode.BUILD,
            strict_mode=True,
        )
        verdict = jev.evaluate_tool_call(
            tool_name="read_file",
            tool_category=ToolCategory.FILESYSTEM,
        )
        assert verdict.decision is JevDecision.ASK

    def test_get_jev_singleton(self) -> None:
        jev1 = get_jev()
        jev2 = get_jev()
        assert jev1 is jev2

    def test_reset_jev(self) -> None:
        jev1 = get_jev()
        reset_jev()
        jev2 = get_jev()
        assert jev1 is not jev2

    def test_destructive_action_asks(self) -> None:
        jev = Jev(environment=Environment.DEVELOPMENT, autonomy_mode=AutonomyMode.AUTO)
        verdict = jev.evaluate_tool_call(
            tool_name="delete_file",
            tool_category=ToolCategory.FILESYSTEM,
            is_destructive=True,
        )
        assert verdict.decision is JevDecision.ASK

    def test_sensitive_category_is_high_risk(self) -> None:
        jev = Jev(environment=Environment.DEVELOPMENT, autonomy_mode=AutonomyMode.BUILD)
        verdict = jev.evaluate_tool_call(
            tool_name="read_secret",
            tool_category=ToolCategory.CREDENTIALS,
        )
        assert verdict.decision is JevDecision.ASK

    def test_ask_scope_triggers_ask(self) -> None:
        jev = Jev(environment=Environment.DEVELOPMENT, autonomy_mode=AutonomyMode.AUTO)
        verdict = jev.evaluate_tool_call(
            tool_name="git_push",
            tool_category=ToolCategory.GIT,
            scope="git:remote",
            is_remote=True,
        )
        assert verdict.decision is JevDecision.ASK


# ---------------------------------------------------------------------------
# Rollback tests
# ---------------------------------------------------------------------------


class TestRollback:
    """Rollback system tests."""

    def test_capture_files(self, tmp_path: Path) -> None:
        from agent_system.services.rollback import get_rollback_manager, reset_rollback_manager

        reset_rollback_manager()
        rb = get_rollback_manager()
        test_file = tmp_path / "test.txt"
        test_file.write_text("original")
        cp = rb.capture_files(
            task_id="task-1",
            operation="test",
            file_paths=[str(test_file)],
        )
        assert cp is not None
        assert cp.checkpoint_type.value == "filesystem"
        assert len(cp.file_snapshots) == 1
        assert cp.file_snapshots[0].content == b"original"

    def test_capture_files_nonexistent(self, tmp_path: Path) -> None:
        from agent_system.services.rollback import get_rollback_manager, reset_rollback_manager

        reset_rollback_manager()
        rb = get_rollback_manager()
        cp = rb.capture_files(
            task_id="task-1",
            operation="test",
            file_paths=[str(tmp_path / "nonexistent.txt")],
        )
        assert cp is not None
        assert cp.file_snapshots[0].existed is False

    def test_rollback_files(self, tmp_path: Path) -> None:
        from agent_system.services.rollback import get_rollback_manager, reset_rollback_manager

        reset_rollback_manager()
        rb = get_rollback_manager()
        test_file = tmp_path / "test.txt"
        test_file.write_text("original")
        cp = rb.capture_files(
            task_id="task-1",
            operation="test",
            file_paths=[str(test_file)],
        )
        assert cp is not None
        test_file.write_text("modified")
        assert rb.commit(cp.id) is True
        assert rb.rollback(cp.id) is True
        assert test_file.read_text() == "original"

    def test_rollback_restores_deleted_file(self, tmp_path: Path) -> None:
        from agent_system.services.rollback import get_rollback_manager, reset_rollback_manager

        reset_rollback_manager()
        rb = get_rollback_manager()
        test_file = tmp_path / "test.txt"
        test_file.write_text("original")
        cp = rb.capture_files(
            task_id="task-1",
            operation="test",
            file_paths=[str(test_file)],
        )
        assert cp is not None
        test_file.unlink()
        assert rb.commit(cp.id) is True
        assert rb.rollback(cp.id) is True
        assert test_file.exists()
        assert test_file.read_text() == "original"

    def test_list_checkpoints(self, tmp_path: Path) -> None:
        from agent_system.services.rollback import get_rollback_manager, reset_rollback_manager

        reset_rollback_manager()
        rb = get_rollback_manager()
        test_file = tmp_path / "test.txt"
        test_file.write_text("original")
        rb.capture_files(task_id="task-1", operation="test", file_paths=[str(test_file)])
        rb.capture_files(task_id="task-2", operation="test", file_paths=[str(test_file)])
        cps = rb.list_checkpoints()
        assert len(cps) == 2
        task1_cps = rb.list_checkpoints("task-1")
        assert len(task1_cps) == 1

    def test_can_rollback(self, tmp_path: Path) -> None:
        from agent_system.services.rollback import get_rollback_manager, reset_rollback_manager

        reset_rollback_manager()
        rb = get_rollback_manager()
        test_file = tmp_path / "test.txt"
        test_file.write_text("original")
        cp = rb.capture_files(
            task_id="task-1",
            operation="test",
            file_paths=[str(test_file)],
        )
        assert cp is not None
        assert rb.can_rollback(cp.id) is False
        rb.commit(cp.id)
        assert rb.can_rollback(cp.id) is True


# ---------------------------------------------------------------------------
# Hooks tests
# ---------------------------------------------------------------------------


class TestHooks:
    """Hook system tests."""

    def test_register_and_fire(self) -> None:
        from agent_system.services.hooks import HookEvent, HookManager

        hm = HookManager()
        calls: list[str] = []

        def my_hook(ctx):
            calls.append(ctx.event.value)
            return ctx

        hm.register(HookEvent.BEFORE_TOOL, my_hook, name="test_hook")
        ctx = hm.fire_sync(HookEvent.BEFORE_TOOL, {"tool": "test"})
        assert len(calls) == 1
        assert ctx.event is HookEvent.BEFORE_TOOL

    def test_unregister(self) -> None:
        from agent_system.services.hooks import HookEvent, HookManager

        hm = HookManager()

        def my_hook(ctx):
            return ctx

        hm.register(HookEvent.BEFORE_TOOL, my_hook, name="test_hook")
        assert hm.unregister(HookEvent.BEFORE_TOOL, "test_hook") is True
        assert hm.unregister(HookEvent.BEFORE_TOOL, "test_hook") is False

    def test_disable_enable(self) -> None:
        from agent_system.services.hooks import HookEvent, HookManager

        hm = HookManager()
        calls: list[str] = []

        def my_hook(ctx):
            calls.append("called")
            return ctx

        hm.register(HookEvent.BEFORE_TOOL, my_hook, name="test_hook")
        hm.disable(HookEvent.BEFORE_TOOL, "test_hook")
        hm.fire_sync(HookEvent.BEFORE_TOOL, {})
        assert len(calls) == 0
        hm.enable(HookEvent.BEFORE_TOOL, "test_hook")
        hm.fire_sync(HookEvent.BEFORE_TOOL, {})
        assert len(calls) == 1

    def test_priority_ordering(self) -> None:
        from agent_system.services.hooks import HookEvent, HookManager

        hm = HookManager()
        order: list[str] = []

        def hook1(ctx):
            order.append("first")
            return ctx

        def hook2(ctx):
            order.append("second")
            return ctx

        hm.register(HookEvent.BEFORE_TOOL, hook2, name="hook2", priority=200)
        hm.register(HookEvent.BEFORE_TOOL, hook1, name="hook1", priority=100)
        hm.fire_sync(HookEvent.BEFORE_TOOL, {})
        assert order == ["first", "second"]

    def test_abort_signal(self) -> None:
        from agent_system.services.hooks import HookContext, HookEvent, HookManager, HookSignal

        hm = HookManager()
        calls: list[str] = []

        def abort_hook(ctx):
            return HookContext(
                event=ctx.event,
                data=ctx.data,
                signal=HookSignal.ABORT,
                abort_reason="test abort",
            )

        def should_not_run(ctx):
            calls.append("should_not_run")
            return ctx

        hm.register(HookEvent.BEFORE_TOOL, abort_hook, name="abort", priority=1)
        hm.register(HookEvent.BEFORE_TOOL, should_not_run, name="after", priority=2)
        ctx = hm.fire_sync(HookEvent.BEFORE_TOOL, {})
        assert ctx.signal is HookSignal.ABORT
        assert len(calls) == 0

    def test_async_fire(self) -> None:
        from agent_system.services.hooks import HookEvent, HookManager

        hm = HookManager()
        calls: list[str] = []

        async def async_hook(ctx):
            calls.append("async_called")
            return ctx

        hm.register(HookEvent.BEFORE_TOOL, async_hook, name="async_hook")
        asyncio.run(hm.fire(HookEvent.BEFORE_TOOL, {}))
        assert len(calls) == 1

    def test_clear(self) -> None:
        from agent_system.services.hooks import HookEvent, HookManager

        hm = HookManager()

        def my_hook(ctx):
            return ctx

        hm.register(HookEvent.BEFORE_TOOL, my_hook, name="test_hook")
        hm.clear(HookEvent.BEFORE_TOOL)
        assert len(hm.list_hooks(HookEvent.BEFORE_TOOL)) == 0

    def test_get_hook_manager_singleton(self) -> None:
        from agent_system.services.hooks import get_hook_manager, reset_hook_manager

        reset_hook_manager()
        hm1 = get_hook_manager()
        hm2 = get_hook_manager()
        assert hm1 is hm2
        reset_hook_manager()


# ---------------------------------------------------------------------------
# Auto Mode tests
# ---------------------------------------------------------------------------


class TestAutoMode:
    """Auto Mode tests."""

    def test_auto_mode_state_transitions(self) -> None:
        from agent_system.services.auto_mode import AutoMode, AutoModeState

        auto = AutoMode()
        assert auto.state is AutoModeState.IDLE
        auto.reset()
        assert auto.state is AutoModeState.IDLE

    def test_check_action_allows_low_risk(self) -> None:
        from agent_system.services.auto_mode import AutoMode
        from agent_system.services.jev import JevDecision, ToolCategory

        auto = AutoMode()
        decision = auto.check_action(
            tool_name="read_file",
            tool_category=ToolCategory.FILESYSTEM,
        )
        assert decision is JevDecision.ALLOW

    def test_check_action_denies_in_plan_mode(self) -> None:
        from agent_system.services.auto_mode import AutoMode
        from agent_system.services.jev import JevDecision, ToolCategory
        from agent_system.services.permissions import AutonomyMode

        auto = AutoMode(autonomy_mode=AutonomyMode.PLAN)
        decision = auto.check_action(
            tool_name="write_file",
            tool_category=ToolCategory.FILESYSTEM,
        )
        assert decision is JevDecision.DENY

    def test_should_verify_high_risk(self) -> None:
        from agent_system.services.auto_mode import AutoMode
        from agent_system.services.permissions import Risk

        auto = AutoMode()
        assert auto.should_verify(Risk.HIGH) is True
        assert auto.should_verify(Risk.LOW) is False

    def test_should_verify_in_production(self) -> None:
        from agent_system.services.auto_mode import AutoMode
        from agent_system.services.jev import Environment
        from agent_system.services.permissions import Risk

        auto = AutoMode(environment=Environment.PRODUCTION)
        assert auto.should_verify(Risk.LOW) is True

    def test_get_auto_mode_singleton(self) -> None:
        from agent_system.services.auto_mode import get_auto_mode, reset_auto_mode

        reset_auto_mode()
        am1 = get_auto_mode()
        am2 = get_auto_mode()
        assert am1 is am2
        reset_auto_mode()


# ---------------------------------------------------------------------------
# Verification Loop tests
# ---------------------------------------------------------------------------


class TestVerificationLoop:
    """Verification loop tests."""

    def test_check_types(self) -> None:
        from agent_system.services.verification_loop import CheckType

        assert CheckType.LINT.value == "lint"
        assert CheckType.TYPECHECK.value == "typecheck"
        assert CheckType.UNIT_TEST.value == "unit_test"

    def test_verification_report(self) -> None:
        from agent_system.services.verification_loop import (
            CheckResult,
            CheckStatus,
            CheckType,
            VerificationReport,
        )

        report = VerificationReport(task_id="task-1", goal="test")
        report.checks.append(CheckResult(check_type=CheckType.LINT, status=CheckStatus.PASSED))
        report.checks.append(CheckResult(check_type=CheckType.TYPECHECK, status=CheckStatus.FAILED))
        assert report.passed_checks == 1
        assert report.failed_checks == 1
        assert report.all_passed is False
        report.needs_fix = any(c.status == CheckStatus.FAILED for c in report.checks)
        assert report.needs_fix is True

    def test_verification_loop_skips_missing_tools(self, tmp_path: Path) -> None:
        from agent_system.services.verification_loop import VerificationLoop

        vl = VerificationLoop(
            workspace_path=str(tmp_path),
            enable_lint=True,
            enable_typecheck=True,
            enable_tests=True,
            enable_security=False,
        )
        report = vl.verify("task-1", "test goal")
        assert len(report.checks) > 0

    def test_verification_loop_disabled_checks(self, tmp_path: Path) -> None:
        from agent_system.services.verification_loop import CheckStatus, VerificationLoop

        vl = VerificationLoop(
            workspace_path=str(tmp_path),
            enable_lint=False,
            enable_typecheck=False,
            enable_tests=False,
            enable_security=False,
        )
        report = vl.verify("task-1", "test goal")
        skipped = [c for c in report.checks if c.status is CheckStatus.SKIPPED]
        assert len(skipped) == 5
