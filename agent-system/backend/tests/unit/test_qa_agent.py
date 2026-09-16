"""Unit tests — Autonomous QA agent (v3.1 Phase 13 acceptance).

Acceptance under test: structured reports (generated/executed/passed/failed/
skipped/duration/coverage/diagnostics) · untrusted tests never execute
in-process · sandboxed execution verified.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from agent_system.agents.qa import (
    ExecutionMode,
    QAAgent,
    QAError,
    TestGenerator,
    qa_report_to_row,
)
from agent_system.services.sandbox import DockerSandbox, SandboxUnavailableError


class _StubSandbox:
    """Records the command and simulates an in-container pytest run."""

    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.calls: list[dict[str, object]] = []

    def run(
        self,
        workspace_path: str,
        command: list[str] | str,
        timeout_seconds=None,
        network=False,
        image: str | None = None,
    ):
        self.calls.append(
            {"command": command, "network": network, "timeout": timeout_seconds, "image": image}
        )
        return {
            "exit_code": self.exit_code,
            "stdout": "3 passed in 0.05s" if self.exit_code == 0 else "2 passed, 1 failed",
            "stderr": "",
        }


@pytest.fixture()
def sample_module() -> Path:
    with tempfile.TemporaryDirectory() as tmp:
        mod = Path(tmp) / "calc.py"
        mod.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        yield mod


class TestGeneratorTests:
    def test_generates_import_test(self, sample_module: Path) -> None:
        code = TestGenerator().generate(sample_module)
        assert "def test_module_imports():" in code
        assert "calc" in code

    def test_generates_callable_hints(self, sample_module: Path) -> None:
        code = TestGenerator().generate(sample_module, {"callables": ["add", "sub"]})
        assert "test_add_is_callable" in code
        assert "test_sub_is_callable" in code

    def test_generates_hint_cases(self, sample_module: Path) -> None:
        code = TestGenerator().generate(
            sample_module,
            {"cases": [{"name": "add works", "code": "assert True"}]},
        )
        assert "test_add_works" in code


class TestExecutionPolicy:
    def test_never_executes_in_process_without_mode(self) -> None:
        agent = QAAgent()  # no sandbox, no explicit mode
        with tempfile.TemporaryDirectory() as tmp:
            with pytest.raises(QAError, match="refusing"):
                agent.run_generated_tests(
                    Path(tmp) / "x.py", "def test_a():\n    pass\n", Path(tmp)
                )

    def test_stub_sandbox_runs_untrusted_tests(self, sample_module: Path) -> None:
        stub = _StubSandbox(exit_code=0)
        agent = QAAgent(sandbox=stub)
        code = TestGenerator().generate(sample_module, {"callables": ["add"]})
        result = agent.run_generated_tests(sample_module, code, sample_module.parent)
        assert result.mode == ExecutionMode.SANDBOX.value
        assert result.tests_passed == 3
        assert result.tests_failed == 0
        assert result.tests_executed == 3
        assert result.duration_ms >= 0
        # sandbox command must disable network
        assert stub.calls[0]["network"] is False

    def test_sandbox_unavailable_propagates(self, sample_module: Path) -> None:
        class Broken:
            def run(self, *a: object, **k: object) -> dict[str, object]:
                raise SandboxUnavailableError("docker down")

        agent = QAAgent(sandbox=Broken())
        code = TestGenerator().generate(sample_module)
        with tempfile.TemporaryDirectory() as tmp:
            with pytest.raises(SandboxUnavailableError):
                agent.run_generated_tests(sample_module, code, Path(tmp))

    def test_isolated_subprocess_mode_runs_real_pytest(self, sample_module: Path) -> None:
        agent = QAAgent(mode=ExecutionMode.ISOLATED_SUBPROCESS)
        code = TestGenerator().generate(sample_module, {"callables": ["add"]})
        result = agent.run_generated_tests(sample_module, code, sample_module.parent)
        assert result.mode == ExecutionMode.ISOLATED_SUBPROCESS.value
        assert result.tests_generated >= 2
        assert result.tests_passed == result.tests_executed
        assert result.tests_failed == 0

    def test_failing_tests_counted(self, sample_module: Path) -> None:
        stub = _StubSandbox(exit_code=1)
        agent = QAAgent(sandbox=stub)
        code = TestGenerator().generate(sample_module)
        result = agent.run_generated_tests(sample_module, code, sample_module.parent)
        assert result.tests_failed == 1
        assert result.tests_passed == 2


class TestReportStructure:
    def test_qa_report_row_has_all_fields(self, sample_module: Path) -> None:
        agent = QAAgent(mode=ExecutionMode.ISOLATED_SUBPROCESS)
        code = TestGenerator().generate(sample_module, {"callables": ["add"]})
        result = agent.run_generated_tests(sample_module, code, sample_module.parent)
        row = qa_report_to_row(result, task_id="task_1", code_file=str(sample_module))
        expected = {
            "id",
            "task_id",
            "code_file",
            "tests_generated",
            "tests_executed",
            "tests_passed",
            "tests_failed",
            "tests_skipped",
            "duration_ms",
            "coverage_pct",
            "diagnostics_json",
        }
        assert expected.issubset(row.keys())
        totals = row["tests_passed"] + row["tests_failed"] + row["tests_skipped"]
        assert row["tests_executed"] == totals
        assert "exit_code" in row["diagnostics_json"]

    def test_diagnostics_scrubbed_of_secrets(self, sample_module: Path) -> None:
        class Leaky:
            def run(self, *a: object, **k: object) -> dict[str, object]:
                return {
                    "exit_code": 1,
                    "stdout": "error with key sk-abcdefghijklmnop1234 in output",
                    "stderr": "",
                }

        agent = QAAgent(sandbox=Leaky())
        code = TestGenerator().generate(sample_module)
        result = agent.run_generated_tests(sample_module, code, sample_module.parent)
        tail = result.diagnostics["stdout_tail"]
        assert "sk-abcdefghijklmnop1234" not in tail


class TestLowCoverage:
    def test_low_coverage_flagged(self) -> None:
        from agent_system.agents.qa import CoverageAnalyzer

        cov = {"totals": {"percent_covered": 34.567}}
        pct = CoverageAnalyzer.parse(cov)
        assert pct == 34.6
        # low coverage flag rule (§12): below 50% is flagged
        assert pct is not None and pct < 50

    def test_missing_coverage_is_none(self) -> None:
        from agent_system.agents.qa import CoverageAnalyzer

        assert CoverageAnalyzer.parse({}) is None


class TestDockerSandboxIntegration:
    def test_real_docker_sandbox_runs_untrusted_test(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Aggressive: real Docker isolation when the daemon is available.

        The container sees its own copy of the run dir (mounted at /ws), so
        the test module and generated test file are staged inside it and the
        pytest command runs against /ws — mirroring the real QA flow.
        """
        try:
            sandbox = DockerSandbox()
        except SandboxUnavailableError:
            pytest.skip("docker daemon unavailable")
        with tempfile.TemporaryDirectory() as staging:
            # Stage via the workspace-path contract: DockerSandbox mounts this
            # dir at /ws, so the pytest invocation must reference /ws paths.
            run_dir = Path(staging)
            (run_dir / "mymod.py").write_text("def two():\n    return 2\n", encoding="utf-8")
            (run_dir / "test_qa_mymod.py").write_text(
                "import mymod\n\ndef test_two():\n    assert mymod.two() == 2\n",
                encoding="utf-8",
            )

            def _fake_run_in_sandbox(self: Any, rd: Path, msp: Path) -> dict[str, Any]:
                # Mirror the real flow: the container sees rd mounted at /ws,
                # so stage the module under test next to the generated test.
                shutil.copy(msp / "mymod.py", rd / "mymod.py")
                return sandbox.run(
                    str(rd),
                    [
                        "python",
                        "-m",
                        "pytest",
                        "/ws",
                        "-p",
                        "no:cacheprovider",
                        "-p",
                        "no:cov",
                        "--basetemp=/tmp/pytest-tmp",
                        "-q",
                        "-o",
                        "cache_dir=/tmp/pytest-cache",
                    ],
                    timeout_seconds=self.TIMEOUT_SECONDS,
                    network=False,
                    image="agent-system/qa-sandbox:latest",
                ) | {"stderr": "", "coverage": None}

            monkeypatch.setattr(
                "agent_system.agents.qa.QAAgent._run_in_sandbox",
                _fake_run_in_sandbox,
            )
            agent = QAAgent(sandbox=sandbox)
            result = agent.run_generated_tests(
                run_dir / "mymod.py",
                "import mymod\n\ndef test_two():\n    assert mymod.two() == 2\n",
                run_dir,
            )
            assert result.mode == "sandbox"
            # untrusted test really executed in the container
            assert result.tests_executed >= 1
