"""Autonomous QA agent (v3.1 Phase 13, §12 feature note).

TestGenerator produces pytest test files from a code file + spec hints. The
generated tests are UNTRUSTED CODE: they execute only inside a sandbox when
Docker is available. When Docker is unavailable the QA run raises
`SandboxUnavailableError` upstream of any execution — the caller can fall
back to `ExecutionMode.ISOLATED_SUBPROCESS`, which runs the tests in a bare
subprocess with hard resource limits (documented weaker isolation, no host
credentials, cwd confined to a temp dir). Direct in-process execution of
untrusted tests is never allowed.

Every run persists a structured QAReport (counts, duration, coverage,
diagnostics) and emits qa.* events.
"""

from __future__ import annotations

import json
import resource
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from agent_system.domain import ids
from agent_system.services.memory import scrub_text


class QAError(RuntimeError):
    pass


class ExecutionMode(StrEnum):
    SANDBOX = "sandbox"  # Docker isolation (preferred)
    ISOLATED_SUBPROCESS = "isolated_subprocess"  # resource-limited subprocess (weaker, documented)


TEST_TEMPLATE = '''"""Auto-generated QA tests (untrusted — run sandboxed only)."""
import importlib
import inspect

import pytest

MODULE = "{module_name}"

def _mod():
    return importlib.import_module(MODULE)

{extra_tests}
'''


@dataclass
class QAResult:
    report_id: str
    tests_generated: int
    tests_executed: int
    tests_passed: int
    tests_failed: int
    tests_skipped: int
    duration_ms: int
    coverage_pct: float | None
    mode: str
    diagnostics: dict[str, Any] = field(default_factory=dict)


class TestGenerator:
    """Produces pytest test files from a source file + optional spec hints.

    Generated tests exercise the public surface: importability, callable
    smoke tests, and any hint-driven cases. Output is plain pytest code.
    """

    def generate(self, source_path: Path, hints: dict[str, Any] | None = None) -> str:
        hints = hints or {}
        module_name = source_path.stem
        cases: list[str] = []

        cases.append("def test_module_imports():\n    mod = _mod()\n    assert mod is not None\n")

        callables = hints.get("callables", [])
        for fn_name in callables:
            cases.append(
                f"def test_{fn_name}_is_callable():\n"
                f"    mod = _mod()\n"
                f"    assert hasattr(mod, '{fn_name}')\n"
            )

        for case in hints.get("cases", []):
            name = "".join(c if c.isalnum() else "_" for c in str(case.get("name", "case")))
            body = str(case.get("code", "assert True"))
            cases.append(f"def test_{name}():\n    {body}\n")

        return TEST_TEMPLATE.format(module_name=module_name, extra_tests="\n\n".join(cases))


class CoverageAnalyzer:
    """Parses pytest-cov output into a coverage percentage (None if absent)."""

    @staticmethod
    def parse(cov_json: dict[str, Any]) -> float | None:
        totals = cov_json.get("totals", {})
        pct = totals.get("percent_covered")
        if pct is None:
            # coverage.py JSON: percent_covered_display or compute from files
            files = cov_json.get("files", {})
            if not files:
                return None
            coverages = [f.get("summary", {}).get("percent_covered") for f in files.values()]
            vals = [c for c in coverages if isinstance(c, (int, float))]
            return round(sum(vals) / len(vals), 1) if vals else None
        return round(float(pct), 1)


class QAAgent:
    """Runs generated untrusted tests with sandbox-first execution policy."""

    TIMEOUT_SECONDS = 120

    def __init__(self, sandbox: Any | None = None, mode: ExecutionMode | None = None) -> None:
        self._sandbox = sandbox
        self._mode = mode

    def _resolve_mode(self) -> ExecutionMode:
        if self._mode is not None:
            return self._mode
        if self._sandbox is not None:
            return ExecutionMode.SANDBOX
        raise QAError(
            "no execution mode available: no sandbox configured and no explicit "
            "mode override given — refusing to run untrusted tests in-process"
        )

    def run_generated_tests(
        self,
        source_path: Path,
        test_code: str,
        module_search_path: Path,
    ) -> QAResult:
        import time

        started = time.monotonic()
        mode = self._resolve_mode()

        with tempfile.TemporaryDirectory(prefix="qa_run_") as tmp:
            tmp_dir = Path(tmp)
            test_file = tmp_dir / f"test_qa_{source_path.stem}.py"
            if mode == ExecutionMode.SANDBOX:
                # Generated tests import the module under test by name; make
                # /ws explicit so container cwd assumptions never matter.
                test_file.write_text(
                    "import sys; sys.path.insert(0, '/ws')\n" + test_code,
                    encoding="utf-8",
                )
                outcome = self._run_in_sandbox(tmp_dir, module_search_path)
            else:
                test_file.write_text(test_code, encoding="utf-8")
                outcome = self._run_in_subprocess(tmp_dir, module_search_path)

        duration_ms = int((time.monotonic() - started) * 1000)
        parsed = self._parse_pytest_output(outcome["stdout"] + outcome["stderr"])
        coverage = None
        if outcome.get("coverage"):
            coverage = CoverageAnalyzer.parse(outcome["coverage"])

        report_id = ids.new_qa_report_id()
        return QAResult(
            report_id=report_id,
            tests_generated=test_code.count("def test_"),
            tests_executed=parsed["executed"],
            tests_passed=parsed["passed"],
            tests_failed=parsed["failed"],
            tests_skipped=parsed["skipped"],
            duration_ms=duration_ms,
            coverage_pct=coverage,
            mode=mode.value,
            diagnostics={
                "exit_code": outcome["exit_code"],
                "stdout_tail": scrub_text(outcome["stdout"][-2000:]),
                "stderr_tail": scrub_text(outcome["stderr"][-2000:]),
            },
        )

    def _run_in_sandbox(self, run_dir: Path, module_search_path: Path) -> dict[str, Any]:
        if self._sandbox is None:
            from agent_system.services.sandbox import DockerSandbox

            self._sandbox = DockerSandbox()
        # The container sees run_dir mounted at /ws (not the host path), so
        # stage the module(s) under test next to the generated test file and
        # target /ws. Bounded: top-level *.py only, never the whole tree.
        import shutil

        try:
            search = Path(module_search_path)
            if search.is_dir():
                for src in sorted(search.glob("*.py")):
                    dest = Path(run_dir) / src.name
                    if not dest.exists():
                        shutil.copy(src, dest)
        except OSError:
            pass  # staging best-effort; pytest reports a missing import honestly
        command = [
            "python",
            "-m",
            "pytest",
            "/ws",
            "-p",
            "no:cacheprovider",
            "--tb=short",
            "-q",
        ]
        result = self._sandbox.run(
            str(run_dir),
            command,
            timeout_seconds=self.TIMEOUT_SECONDS,
            network=False,
            image="agent-system/qa-sandbox:latest",
        )
        # DockerSandbox returns {exit_code, stdout, timed_out}; normalize shape.
        result.setdefault("stderr", "")
        result["coverage"] = None
        return result

    def _run_in_subprocess(self, run_dir: Path, module_search_path: Path) -> dict[str, Any]:
        """Weaker-but-honest isolation: subprocess + rlimits, no host creds.

        Documented limitations vs Docker: shares host FS permissions, kernel,
        and network namespace. Never used unless explicitly requested.
        """
        env = {
            k: v
            for k, v in (
                ("PATH", "/usr/bin:/bin"),
                ("PYTHONPATH", str(module_search_path)),
                ("HOME", str(run_dir)),
            ).__iter__()
        }

        def _limits() -> None:
            resource.setrlimit(resource.RLIMIT_CPU, (10, 12))
            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
            resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024, 10 * 1024 * 1024))

        try:
            proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    str(run_dir),
                    "-p",
                    "no:cacheprovider",
                    "--tb=short",
                    "-q",
                ],
                cwd=str(run_dir),
                env=env,
                capture_output=True,
                text=True,
                timeout=self.TIMEOUT_SECONDS,
                preexec_fn=_limits,
            )
            return {
                "exit_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "coverage": None,
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "exit_code": -1,
                "stdout": exc.stdout or "",
                "stderr": f"timed out after {self.TIMEOUT_SECONDS}s",
                "coverage": None,
            }

    @staticmethod
    def _parse_pytest_output(output: str) -> dict[str, int]:
        executed = passed = failed = skipped = 0
        for line in output.splitlines():
            line = line.strip()
            # pytest -q summary like "5 passed in 0.02s" or "3 passed, 1 failed"
            if "passed" in line or "failed" in line or "skipped" in line:
                import re

                passed_m = re.search(r"(\d+) passed", line)
                failed_m = re.search(r"(\d+) failed", line)
                skipped_m = re.search(r"(\d+) skipped", line)
                passed = int(passed_m.group(1)) if passed_m else 0
                failed = int(failed_m.group(1)) if failed_m else 0
                skipped = int(skipped_m.group(1)) if skipped_m else 0
                executed = passed + failed + skipped
                break
        return {
            "executed": executed,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
        }


def qa_report_to_row(
    result: QAResult, task_id: str | None, code_file: str | None
) -> dict[str, Any]:
    """Flatten a QAResult for QAReport persistence (infra/models.QAReport)."""
    return {
        "id": result.report_id,
        "task_id": task_id,
        "code_file": code_file,
        "tests_generated": result.tests_generated,
        "tests_executed": result.tests_executed,
        "tests_passed": result.tests_passed,
        "tests_failed": result.tests_failed,
        "tests_skipped": result.tests_skipped,
        "duration_ms": result.duration_ms,
        "coverage_pct": result.coverage_pct,
        "diagnostics_json": json.loads(json.dumps(result.diagnostics)),
    }
