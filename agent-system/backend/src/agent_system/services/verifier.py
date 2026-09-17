"""Verifier — post-execution REVIEW gate (diagram: EXECUTOR -> VERIFIER).

Runs after a task handler succeeds, while the task is in REVIEW:

- Deterministic checks first (explicit failure signals in the result payload,
  e.g. ``tests_failed > 0`` or ``verified: False``).
- Opt-in QAAgent execution when the result carries a ``verify_qa`` payload
  (``source_path`` + ``test_code`` + ``module_search_path``): untrusted tests
  run sandboxed, a QAReport row is persisted, and failures fail the task.
- Opt-in LLM judge when ``verifier_use_llm_judge`` is enabled and a real
  provider is configured: the Model Layer answers PASS/FAIL JSON.
- Lenient by default: tasks without verifiable artifacts pass with a recorded
  reason, so REVIEW is exercised (no longer orphaned) without breaking
  existing flows. ``verifier_strict=True`` turns unevidenced passes into
  failures.

The verifier never raises: crashes return a pass (lenient) or fail (strict)
with the crash recorded, so a broken verifier cannot wedge the Orchestrator.
"""

from __future__ import annotations

import json as _json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ECHO_PROVIDERS = {"echo", "none", ""}

JUDGE_SYSTEM_PROMPT = (
    "You judge whether a task result satisfies its goal. "
    "Reply with ONLY a JSON object, no prose: "
    '{"verdict": "PASS"|"FAIL", "reason": str}. '
    "FAIL only when the output clearly misses the goal or reports an error."
)


@dataclass
class VerificationResult:
    passed: bool
    reason: str
    mode: str = "lenient"  # lenient | strict | qa | llm_judge | off | deterministic
    qa_result: Any | None = None
    details: dict[str, Any] = field(default_factory=dict)


class Verifier:
    """Post-execution gate; construct once per Orchestrator/worker."""

    def __init__(self, settings: Any = None, model_router: Any = None) -> None:
        self._settings = settings
        self._router = model_router

    @property
    def enabled(self) -> bool:
        if self._settings is None:
            return True
        return bool(getattr(self._settings, "verifier_enabled", True))

    @property
    def strict(self) -> bool:
        if self._settings is None:
            return False
        return bool(getattr(self._settings, "verifier_strict", False))

    def verify(
        self,
        task_input: dict[str, Any],
        result: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> VerificationResult:
        """Check one successful handler result. Never raises."""
        try:
            if not self.enabled:
                return VerificationResult(True, "verifier disabled", mode="off")
            deterministic = self._check_deterministic(task_input, result)
            if deterministic is not None:
                return deterministic
            qa_payload = (result or {}).get("verify_qa")
            if isinstance(qa_payload, dict):
                return self._run_qa(task_input, result, qa_payload, context)
            judge = self._try_llm_judge(task_input, result, context)
            if judge is not None:
                return judge
            if self.strict:
                return VerificationResult(
                    False,
                    "strict mode: no verifiable artifact or judge available",
                    mode="strict",
                )
            return VerificationResult(
                True, "no verifiable artifact; lenient pass", mode="lenient"
            )
        except Exception as exc:  # verifier crash must not wedge execution
            if self.strict:
                return VerificationResult(
                    False, f"verifier crashed: {type(exc).__name__}: {exc}", mode="strict"
                )
            return VerificationResult(
                True, f"verifier crashed; lenient pass: {type(exc).__name__}", mode="lenient"
            )

    # -- deterministic signals -------------------------------------------

    def _check_deterministic(
        self, task_input: dict[str, Any], result: dict[str, Any]
    ) -> VerificationResult | None:
        result = result or {}
        if result.get("verified") is False:
            return VerificationResult(
                False,
                str(result.get("verification_reason") or "handler reported verified=false"),
                mode="deterministic",
            )
        for key in ("tests_failed", "failed"):
            value = result.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return VerificationResult(
                    False, f"result reports {key}={value}", mode="deterministic"
                )
        if isinstance(result.get("qa"), dict):
            qa = result["qa"]
            failed = qa.get("tests_failed", 0)
            if isinstance(failed, (int, float)) and failed > 0:
                return VerificationResult(
                    False, f"embedded qa reports tests_failed={failed}", mode="deterministic"
                )
        status = str(result.get("status") or "").upper()
        if status in ("FAIL", "FAILED", "ERROR"):
            return VerificationResult(
                False, f"result status={status}", mode="deterministic"
            )
        return None  # no deterministic signal -> continue to QA / judge / lenient

    # -- QAAgent path ------------------------------------------------------

    def _run_qa(
        self,
        task_input: dict[str, Any],
        result: dict[str, Any],
        qa_payload: dict[str, Any],
        context: dict[str, Any] | None,
    ) -> VerificationResult:
        from agent_system.agents.qa import ExecutionMode, QAAgent, qa_report_to_row

        try:
            source = Path(str(qa_payload.get("source_path") or "")).expanduser()
            test_code = str(qa_payload.get("test_code") or "")
            search = Path(str(qa_payload.get("module_search_path") or ".")).expanduser()
            if not test_code or not source.name:
                raise ValueError("verify_qa needs source_path + test_code")
            mode_raw = str(qa_payload.get("mode") or "").strip()
            if mode_raw == ExecutionMode.ISOLATED_SUBPROCESS.value:
                agent = QAAgent(mode=ExecutionMode.ISOLATED_SUBPROCESS)
            else:
                agent = QAAgent()  # sandbox when Docker is available
                if agent._mode is None and agent._sandbox is None:  # noqa: SLF001
                    try:
                        from agent_system.services.sandbox import DockerSandbox

                        agent = QAAgent(sandbox=DockerSandbox())
                    except Exception:
                        pass
            qa_result = agent.run_generated_tests(source, test_code, search)
        except Exception as exc:
            if self.strict:
                return VerificationResult(
                    False, f"qa verification errored: {type(exc).__name__}: {exc}", mode="qa"
                )
            return VerificationResult(
                True, f"qa unavailable; lenient pass: {type(exc).__name__}", mode="qa"
            )
        # Persist the QAReport (best effort; never fails verification on DB error).
        try:
            factory = (context or {}).get("factory")
            task_id = (context or {}).get("task_id")
            if factory is not None:
                from agent_system.infra.db import session_scope
                from agent_system.infra.models import QAReport

                row = qa_report_to_row(
                    qa_result, task_id=task_id, code_file=str(source)
                )
                with session_scope(factory) as db:
                    db.add(QAReport(**row))
        except Exception:
            pass
        if qa_result.tests_failed > 0 or qa_result.tests_executed == 0:
            return VerificationResult(
                False,
                f"qa: {qa_result.tests_passed}/{qa_result.tests_executed} passed "
                f"({qa_result.tests_failed} failed)",
                mode="qa",
                qa_result=qa_result,
                details={"report_id": qa_result.report_id},
            )
        return VerificationResult(
            True,
            f"qa: {qa_result.tests_passed}/{qa_result.tests_executed} passed",
            mode="qa",
            qa_result=qa_result,
            details={"report_id": qa_result.report_id},
        )

    # -- LLM judge path ----------------------------------------------------

    def _judge_enabled(self) -> bool:
        if self._settings is None:
            return False
        if not bool(getattr(self._settings, "verifier_use_llm_judge", False)):
            return False
        provider = str(getattr(self._settings, "default_provider", "echo") or "echo")
        return provider.strip().lower() not in _ECHO_PROVIDERS

    def _try_llm_judge(
        self,
        task_input: dict[str, Any],
        result: dict[str, Any],
        context: dict[str, Any] | None,
    ) -> VerificationResult | None:
        if not self._judge_enabled():
            return None
        try:
            router = self._router or self._build_router()
            factory = (context or {}).get("factory")
            if router is None or factory is None:
                return None
            model_id = self._resolve_model_id()
            goal = str((task_input or {}).get("goal") or (task_input or {}).get("title") or "")[:2000]
            output = _json.dumps(result or {}, default=str)[:4000]
            prompt = (
                f"{JUDGE_SYSTEM_PROMPT}\n\nGoal:\n{goal}\n\nResult:\n{output}\n\nJSON:"
            )
            invocation = router.invoke(
                factory,
                model_id,
                prompt,
                session_id=(context or {}).get("session_id"),
                task_id=(context or {}).get("task_id"),
                agent_run_id=(context or {}).get("agent_run_id"),
                agent_type="verifier",
            )
            if not invocation.ok or not invocation.output:
                return None
            verdict = _extract_verdict(invocation.output)
            if verdict is None:
                return None  # unparseable judge -> lenient path continues
            passed = verdict[0].upper() == "PASS"
            return VerificationResult(passed, verdict[1][:500], mode="llm_judge")
        except Exception:
            return None

    def _build_router(self) -> Any | None:
        try:
            from agent_system.infra.event_bus import EventBus
            from agent_system.services.providers import build_model_router, configured_providers

            settings = self._settings
            configured = [p["key"] for p in configured_providers(settings) if p["configured"]]
            if not configured:
                return None
            return build_model_router(EventBus(), settings, provider_names=configured)
        except Exception:
            return None

    def _resolve_model_id(self) -> str:
        try:
            from agent_system.services.providers import default_model_id

            return str(default_model_id(self._settings))
        except Exception:
            return "echo-default"


def _extract_verdict(output: str) -> tuple[str, str] | None:
    text = output.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    raw = fenced.group(1) if fenced else text[text.find("{") : text.rfind("}") + 1]
    try:
        data = _json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    verdict = str(data.get("verdict") or "").upper()
    if verdict not in ("PASS", "FAIL"):
        return None
    return verdict, str(data.get("reason") or verdict)


__all__ = ["JUDGE_SYSTEM_PROMPT", "VerificationResult", "Verifier"]
