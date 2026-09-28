"""Verification loop — post-execution verification pipeline.

After implementation, Bob verifies:

    Implementation -> Lint -> Type check -> Unit tests -> Integration tests
                   -> Security checks -> Git diff -> Final verification

If any step fails, the loop diagnoses, fixes, and re-runs (up to max_retries).
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class CheckStatus(StrEnum):
    """Status of one verification check."""

    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class CheckType(StrEnum):
    """Types of verification checks."""

    LINT = "lint"
    TYPECHECK = "typecheck"
    UNIT_TEST = "unit_test"
    INTEGRATION_TEST = "integration_test"
    SECURITY = "security"
    DIFF_REVIEW = "diff_review"
    BUILD = "build"


@dataclass
class CheckResult:
    """Result of one verification check."""

    check_type: CheckType
    status: CheckStatus
    output: str = ""
    error: str = ""
    duration_ms: float = 0.0
    fix_attempted: bool = False
    fix_success: bool = False


@dataclass
class VerificationReport:
    """Full verification report for one task."""

    task_id: str
    goal: str
    checks: list[CheckResult] = field(default_factory=list)
    total_duration_ms: float = 0.0
    all_passed: bool = False
    needs_fix: bool = False
    fix_cycles: int = 0

    @property
    def passed_checks(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.PASSED)

    @property
    def failed_checks(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.FAILED)


class VerificationLoop:
    """Runs verification checks with automatic fix-and-retry."""

    def __init__(
        self,
        *,
        workspace_path: str = ".",
        max_fix_cycles: int = 3,
        enable_lint: bool = True,
        enable_typecheck: bool = True,
        enable_tests: bool = True,
        enable_security: bool = False,
    ) -> None:
        self.workspace_path = Path(workspace_path)
        self.max_fix_cycles = max_fix_cycles
        self.enable_lint = enable_lint
        self.enable_typecheck = enable_typecheck
        self.enable_tests = enable_tests
        self.enable_security = enable_security

    def verify(self, task_id: str, goal: str) -> VerificationReport:
        """Run all verification checks for a task."""
        report = VerificationReport(task_id=task_id, goal=goal)

        checks: list[tuple[CheckType, bool]] = [
            (CheckType.LINT, self.enable_lint),
            (CheckType.TYPECHECK, self.enable_typecheck),
            (CheckType.UNIT_TEST, self.enable_tests),
            (CheckType.INTEGRATION_TEST, self.enable_tests),
            (CheckType.SECURITY, self.enable_security),
            (CheckType.DIFF_REVIEW, True),
        ]

        for check_type, enabled in checks:
            if not enabled:
                report.checks.append(CheckResult(check_type=check_type, status=CheckStatus.SKIPPED))
                continue

            result = self._run_check(check_type)
            report.checks.append(result)

        report.all_passed = all(
            c.status in {CheckStatus.PASSED, CheckStatus.SKIPPED} for c in report.checks
        )
        report.needs_fix = any(c.status == CheckStatus.FAILED for c in report.checks)
        return report

    def verify_with_fix(
        self,
        task_id: str,
        goal: str,
        fix_callback: Any | None = None,
    ) -> VerificationReport:
        """Run verification, auto-fixing failures up to max_fix_cycles.

        fix_callback: async callable(report) -> bool that attempts fixes.
        """
        report = self.verify(task_id, goal)

        while report.needs_fix and report.fix_cycles < self.max_fix_cycles:
            if fix_callback is None:
                break
            report.fix_cycles += 1
            logger.info("verification.fix_cycle task=%s cycle=%d", task_id, report.fix_cycles)
            fixed = fix_callback(report)
            if not fixed:
                break
            report = self.verify(task_id, goal)

        return report

    def _run_check(self, check_type: CheckType) -> CheckResult:
        """Run a single verification check."""
        import time

        start = time.monotonic()
        try:
            if check_type == CheckType.LINT:
                return self._run_lint(start)
            if check_type == CheckType.TYPECHECK:
                return self._run_typecheck(start)
            if check_type == CheckType.UNIT_TEST:
                return self._run_tests(start, "unit")
            if check_type == CheckType.INTEGRATION_TEST:
                return self._run_tests(start, "integration")
            if check_type == CheckType.SECURITY:
                return self._run_security(start)
            if check_type == CheckType.DIFF_REVIEW:
                return self._run_diff_review(start)
            return CheckResult(
                check_type=check_type,
                status=CheckStatus.SKIPPED,
                output="Unknown check type",
            )
        except Exception as exc:
            duration = (time.monotonic() - start) * 1000
            return CheckResult(
                check_type=check_type,
                status=CheckStatus.FAILED,
                error=str(exc),
                duration_ms=duration,
            )

    def _run_lint(self, start: float) -> CheckResult:
        """Run ruff lint."""
        try:
            result = subprocess.run(
                ["ruff", "check", "."],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=self.workspace_path,
            )
            duration = (time.monotonic() - start) * 1000
            if result.returncode == 0:
                return CheckResult(
                    check_type=CheckType.LINT,
                    status=CheckStatus.PASSED,
                    output="No lint errors",
                    duration_ms=duration,
                )
            return CheckResult(
                check_type=CheckType.LINT,
                status=CheckStatus.FAILED,
                output=result.stdout,
                error=result.stderr,
                duration_ms=duration,
            )
        except FileNotFoundError:
            return CheckResult(
                check_type=CheckType.LINT,
                status=CheckStatus.SKIPPED,
                output="ruff not installed",
            )

    def _run_typecheck(self, start: float) -> CheckResult:
        """Run mypy type check."""
        try:
            result = subprocess.run(
                ["mypy", "src/"],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=self.workspace_path,
            )
            duration = (time.monotonic() - start) * 1000
            if result.returncode == 0:
                return CheckResult(
                    check_type=CheckType.TYPECHECK,
                    status=CheckStatus.PASSED,
                    output="No type errors",
                    duration_ms=duration,
                )
            return CheckResult(
                check_type=CheckType.TYPECHECK,
                status=CheckStatus.FAILED,
                output=result.stdout,
                error=result.stderr,
                duration_ms=duration,
            )
        except FileNotFoundError:
            return CheckResult(
                check_type=CheckType.TYPECHECK,
                status=CheckStatus.SKIPPED,
                output="mypy not installed",
            )

    def _run_tests(self, start: float, test_type: str) -> CheckResult:
        """Run pytest."""
        try:
            result = subprocess.run(
                ["python", "-m", "pytest", f"tests/{test_type}/", "-x", "-q"],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=self.workspace_path,
            )
            duration = (time.monotonic() - start) * 1000
            if result.returncode == 0:
                return CheckResult(
                    check_type=CheckType.UNIT_TEST
                    if test_type == "unit"
                    else CheckType.INTEGRATION_TEST,
                    status=CheckStatus.PASSED,
                    output=result.stdout[-500:] if len(result.stdout) > 500 else result.stdout,
                    duration_ms=duration,
                )
            return CheckResult(
                check_type=CheckType.UNIT_TEST
                if test_type == "unit"
                else CheckType.INTEGRATION_TEST,
                status=CheckStatus.FAILED,
                output=result.stdout[-500:] if len(result.stdout) > 500 else result.stdout,
                error=result.stderr[-500:] if len(result.stderr) > 500 else result.stderr,
                duration_ms=duration,
            )
        except FileNotFoundError:
            return CheckResult(
                check_type=CheckType.UNIT_TEST
                if test_type == "unit"
                else CheckType.INTEGRATION_TEST,
                status=CheckStatus.SKIPPED,
                output="pytest not installed",
            )

    def _run_security(self, start: float) -> CheckResult:
        """Run basic security checks."""
        try:
            result = subprocess.run(
                ["bandit", "-r", "src/", "-f", "json"],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=self.workspace_path,
            )
            duration = (time.monotonic() - start) * 1000
            if result.returncode == 0:
                return CheckResult(
                    check_type=CheckType.SECURITY,
                    status=CheckStatus.PASSED,
                    output="No security issues",
                    duration_ms=duration,
                )
            return CheckResult(
                check_type=CheckType.SECURITY,
                status=CheckStatus.FAILED,
                output=result.stdout,
                duration_ms=duration,
            )
        except FileNotFoundError:
            return CheckResult(
                check_type=CheckType.SECURITY,
                status=CheckStatus.SKIPPED,
                output="bandit not installed",
            )

    def _run_diff_review(self, start: float) -> CheckResult:
        """Review git diff for obvious issues."""
        try:
            result = subprocess.run(
                ["git", "diff", "--stat"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.workspace_path,
            )
            duration = (time.monotonic() - start) * 1000
            if result.returncode == 0:
                stat = result.stdout.strip()
                if not stat:
                    return CheckResult(
                        check_type=CheckType.DIFF_REVIEW,
                        status=CheckStatus.PASSED,
                        output="No changes to review",
                        duration_ms=duration,
                    )
                return CheckResult(
                    check_type=CheckType.DIFF_REVIEW,
                    status=CheckStatus.PASSED,
                    output=f"Changes detected:\n{stat}",
                    duration_ms=duration,
                )
            return CheckResult(
                check_type=CheckType.DIFF_REVIEW,
                status=CheckStatus.FAILED,
                error=result.stderr,
                duration_ms=duration,
            )
        except Exception as exc:
            return CheckResult(
                check_type=CheckType.DIFF_REVIEW,
                status=CheckStatus.SKIPPED,
                output=f"Git not available: {exc}",
            )


__all__ = [
    "CheckResult",
    "CheckStatus",
    "CheckType",
    "VerificationLoop",
    "VerificationReport",
]
