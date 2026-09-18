"""Resilient invocation: fallback across providers under one worker (v1).

Fallback semantics (the critical rule):

- SAME task_id / worker_id / execution context / tool state / memory.
- Only provider / model / attempt_id change per attempt.
- Attempts persist in ``worker_attempts`` (durable ledger).
- Tool failures, permission denials and cancellation NEVER switch providers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FallbackOutcome:
    ok: bool
    output: str | None = None
    provider: str = ""
    model_id: str = ""
    attempt_no: int = 0
    attempts: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    stopped_reason: str = ""


def _ledger_enabled(factory: Any) -> bool:
    try:
        from agent_system.infra.models import WorkerAttempt

        with factory() as probe:
            probe.query(WorkerAttempt).limit(1).all()
        return True
    except Exception:
        return False


def _record_attempt(
    factory: Any,
    task_id: str,
    worker_id: str,
    attempt_no: int,
    provider: str,
    model_id: str,
    status: str,
    error: str = "",
    latency_ms: int = 0,
    tool_calls_made: int = 0,
) -> None:
    try:
        from agent_system.domain import ids
        from agent_system.domain.events import utcnow
        from agent_system.infra.db import session_scope
        from agent_system.infra.models import WorkerAttempt

        with session_scope(factory) as db:
            db.add(
                WorkerAttempt(
                    id=ids.new_id("attempt"),
                    task_id=task_id,
                    worker_id=worker_id,
                    attempt_no=attempt_no,
                    provider=provider,
                    model_id=model_id,
                    status=status,
                    error=error[:500] if error else None,
                    latency_ms=latency_ms,
                    tool_calls_made=tool_calls_made,
                    completed_at=utcnow() if status != "started" else None,
                )
            )
    except Exception:
        pass


def _emit_fallback(
    bus: Any,
    factory: Any,
    session_id: str | None,
    task_id: str | None,
    agent_run_id: str | None,
    agent_type: str,
    provider: str,
    model_id: str,
    error: str,
    attempt_no: int,
) -> None:
    if bus is None:
        return
    try:
        from agent_system.domain.events import Event
        from agent_system.infra.db import session_scope

        with session_scope(factory) as db:
            bus.emit(
                Event(
                    type="router.fallback",
                    session_id=session_id,
                    task_id=task_id,
                    agent_run_id=agent_run_id,
                    actor=agent_type,
                    payload={
                        "from_provider": provider,
                        "from_model": model_id,
                        "error": str(error)[:300],
                        "attempt_no": attempt_no,
                    },
                ),
                db,
            )
    except Exception:
        pass


def _emit_exhausted(
    bus: Any,
    factory: Any,
    session_id: str | None,
    task_id: str | None,
    agent_run_id: str | None,
    agent_type: str,
    reason: str,
    attempts: int,
) -> None:
    if bus is None:
        return
    try:
        from agent_system.domain.events import Event
        from agent_system.infra.db import session_scope

        with session_scope(factory) as db:
            bus.emit(
                Event(
                    type="router.exhausted",
                    session_id=session_id,
                    task_id=task_id,
                    agent_run_id=agent_run_id,
                    actor=agent_type,
                    payload={"reason": reason, "attempts": attempts},
                ),
                db,
            )
    except Exception:
        pass


def _emit_selected(
    bus: Any,
    factory: Any,
    session_id: str | None,
    task_id: str | None,
    agent_run_id: str | None,
    agent_type: str,
    provider: str,
    model_id: str,
    attempt_no: int,
    attempt_id: str,
) -> None:
    if bus is None:
        return
    try:
        from agent_system.domain.events import Event
        from agent_system.infra.db import session_scope

        with session_scope(factory) as db:
            bus.emit(
                Event(
                    type="router.selected",
                    session_id=session_id,
                    task_id=task_id,
                    agent_run_id=agent_run_id,
                    actor=agent_type,
                    payload={
                        "provider": provider,
                        "model_id": model_id,
                        "attempt_no": attempt_no,
                        "attempt_id": attempt_id,
                    },
                ),
                db,
            )
    except Exception:
        pass


def _failure_result(provider: str, model_id: str, error: str, latency: int, attempt_id: str) -> Any:
    from agent_system.services.model_router import InvocationResult

    return InvocationResult(
        model_call_id=f"fallback-{attempt_id}",
        model_id=model_id,
        provider=provider,
        ok=False,
        tokens_in=None,
        tokens_out=None,
        tokens_cached=None,
        usage_is_estimated=True,
        cost_usd=None,
        cost_is_estimated=True,
        latency_ms=latency,
        error=error[:500],
    )


def _attempt_failed_ledger(
    factory: Any,
    task_id: str | None,
    worker_id: str | None,
    attempt_no: int,
    provider: str,
    model_id: str,
    err: str,
    lat: int,
) -> None:
    if task_id and worker_id and _ledger_enabled(factory):
        _record_attempt(
            factory,
            str(task_id),
            str(worker_id),
            attempt_no,
            provider,
            model_id,
            "failed",
            err,
            lat,
        )


def _attempt_ok_ledger(
    factory: Any,
    task_id: str | None,
    worker_id: str | None,
    attempt_no: int,
    provider: str,
    model_id: str,
    lat: int,
    tool_calls: int,
) -> None:
    if task_id and worker_id and _ledger_enabled(factory):
        _record_attempt(
            factory,
            str(task_id),
            str(worker_id),
            attempt_no,
            provider,
            model_id,
            "ok",
            "",
            lat,
            tool_calls,
        )


def _report_failure(health: Any, provider: str, model_id: str, err: str) -> None:
    if health is not None:
        try:
            health.report_failure(provider, model_id, err)
        except Exception:
            pass


def _report_success(health: Any, provider: str, model_id: str) -> None:
    if health is not None:
        try:
            health.report_success(provider, model_id)
        except Exception:
            pass


def _call_once(
    router: Any,
    factory: Any,
    model_id: str,
    prompt: str,
    session_id: str | None,
    task_id: str | None,
    agent_run_id: str | None,
    agent_type: str,
    stream: bool,
) -> tuple[Any, str | None]:
    try:
        if stream and hasattr(router, "invoke_streaming"):
            result = router.invoke_streaming(
                factory,
                model_id,
                prompt,
                session_id=session_id,
                task_id=task_id,
                agent_run_id=agent_run_id,
                agent_type=agent_type,
            )
        else:
            result = router.invoke(
                factory,
                model_id,
                prompt,
                session_id=session_id,
                task_id=task_id,
                agent_run_id=agent_run_id,
                agent_type=agent_type,
            )
        return result, None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def invoke_with_fallback(
    router: Any,
    factory: Any,
    candidates: list[tuple[str, str]],
    prompt: str,
    task_id: str | None = None,
    worker_id: str | None = None,
    session_id: str | None = None,
    agent_run_id: str | None = None,
    agent_type: str = "llm",
    max_attempts: int = 3,
    health: Any | None = None,
    bus: Any | None = None,
    stream: bool = False,
) -> Any:
    """Try candidates in order; same task/worker/context every attempt."""
    from agent_system.services.fallback_ledger import should_fallback
    from agent_system.services.provider_health import never_fallback_reason

    attempts = list(candidates or [])[: max(1, max_attempts)]
    history: list[dict[str, Any]] = []
    last: Any = None
    ledger_on = bool(task_id and worker_id) and _ledger_enabled(factory)
    for attempt_no, (provider, model_id) in enumerate(attempts, start=1):
        attempt_id = f"{worker_id or task_id or 'worker'}:attempt:{attempt_no}"
        started = time.monotonic()
        if ledger_on and task_id and worker_id:
            _record_attempt(
                factory, str(task_id), str(worker_id), attempt_no, provider, model_id, "started"
            )
        _emit_selected(
            bus,
            factory,
            session_id,
            task_id,
            agent_run_id,
            agent_type,
            provider,
            model_id,
            attempt_no,
            attempt_id,
        )
        result, crashed = _call_once(
            router, factory, model_id, prompt, session_id, task_id, agent_run_id, agent_type, stream
        )
        latency = int((time.monotonic() - started) * 1000)
        if result is None:
            err = crashed or "model invocation crashed"
            _attempt_failed_ledger(
                factory, task_id, worker_id, attempt_no, provider, model_id, err, latency
            )
            _report_failure(health, provider, model_id, err)
            history.append(
                {"provider": provider, "model_id": model_id, "ok": False, "error": err[:300]}
            )
            stop = never_fallback_reason(err)
            if stop is None and should_fallback(err, attempt_no, max_attempts):
                _emit_fallback(
                    bus,
                    factory,
                    session_id,
                    task_id,
                    agent_run_id,
                    agent_type,
                    provider,
                    model_id,
                    err,
                    attempt_no,
                )
                continue
            _emit_exhausted(
                bus,
                factory,
                session_id,
                task_id,
                agent_run_id,
                agent_type,
                stop or "non_provider_error",
                len(history),
            )
            return _failure_result(provider, model_id, err, latency, attempt_id)
        last = result
        if bool(getattr(result, "ok", False)):
            lat = int(getattr(result, "latency_ms", 0) or latency)
            _report_success(health, provider, model_id)
            _attempt_ok_ledger(
                factory,
                task_id,
                worker_id,
                attempt_no,
                provider,
                model_id,
                lat,
                len(getattr(result, "tool_calls", None) or []),
            )
            history.append({"provider": provider, "model_id": model_id, "ok": True})
            try:
                object.__setattr__(result, "fallback_attempts", history)
            except Exception:
                pass
            return result
        err = str(getattr(result, "error", "") or "model invocation failed")
        lat = int(getattr(result, "latency_ms", 0) or latency)
        _attempt_failed_ledger(
            factory, task_id, worker_id, attempt_no, provider, model_id, err, lat
        )
        _report_failure(health, provider, model_id, err)
        history.append(
            {"provider": provider, "model_id": model_id, "ok": False, "error": err[:300]}
        )
        stop = never_fallback_reason(err)
        if stop is None and should_fallback(err, attempt_no, max_attempts):
            _emit_fallback(
                bus,
                factory,
                session_id,
                task_id,
                agent_run_id,
                agent_type,
                provider,
                model_id,
                err,
                attempt_no,
            )
            continue
        _emit_exhausted(
            bus,
            factory,
            session_id,
            task_id,
            agent_run_id,
            agent_type,
            stop or "non_provider_error",
            len(history),
        )
        try:
            object.__setattr__(result, "fallback_attempts", history)
        except Exception:
            pass
        return result
    _emit_exhausted(
        bus,
        factory,
        session_id,
        task_id,
        agent_run_id,
        agent_type,
        "candidates_exhausted",
        len(history),
    )
    if last is not None:
        try:
            object.__setattr__(last, "fallback_attempts", history)
        except Exception:
            pass
    return last


__all__ = ["FallbackOutcome", "invoke_with_fallback"]
