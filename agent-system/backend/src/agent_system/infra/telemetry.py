"""Metrics & tracing — OpenTelemetry instrumentation (optional).

Disabled by default: when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is unset, this
module does nothing but keep tiny in-memory counters (a few dict increments
per call — negligible overhead, zero new runtime dependencies, zero behavior
change). The in-memory ``snapshot()`` also makes every wired metric
assertable in offline tests.

When the endpoint IS set, ``setup_telemetry`` wires:

- an OTLP exporter (traces + metrics) to the configured collector,
- FastAPI + SQLAlchemy + httpx auto-instrumentation (each best-effort: a
  missing instrumentation package warns once, never takes down the server).

Business metrics (counters/histograms, in-memory + OTel mirrors):

- task duration by terminal state (``task.completed`` / ``task.failed``),
- model call latency by provider (+ ok flag),
- tool call count by risk tier,
- approval decision latency by scope,
- cost per session.

Needs the ``telemetry`` extra (``pip install agent-system[telemetry]``) for
the OTLP path; the in-memory path has no extra dependencies.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


class TelemetryError(RuntimeError):
    """Telemetry was requested but cannot be set up (never fatal to callers)."""


@dataclass
class Metrics:
    """Cheap in-memory aggregates with optional OTel mirrors.

    ``record_*`` methods never raise — telemetry must never break execution.
    When OTel is enabled, each record also forwards to the matching
    counter/histogram; otherwise only the in-memory aggregates update.
    """

    _task_durations: dict[str, list[float]] = field(default_factory=dict)
    _model_latencies: dict[str, list[float]] = field(default_factory=dict)
    _tool_calls: dict[str, int] = field(default_factory=dict)
    _approval_latencies: dict[str, list[float]] = field(default_factory=dict)
    _cost_by_session: dict[str, float] = field(default_factory=dict)
    _otel: Any = None

    def _forward(self, instrument: str, value: float, attrs: dict[str, Any]) -> None:
        """Forward to an OTel instrument (lookup inside the guard)."""
        if self._otel is None:
            return
        try:
            handle = self._otel[instrument]
            if value is not None and hasattr(handle, "record"):
                handle.record(float(value), attrs)
            elif hasattr(handle, "add"):
                handle.add(1, attrs)
        except Exception:
            pass

    # -- record ------------------------------------------------------

    def record_task_duration(self, state: str, seconds: float) -> None:
        self._task_durations.setdefault(state, []).append(float(seconds))
        self._forward("task_duration", float(seconds), {"state": state})

    def record_model_latency(self, provider: str, latency_ms: int, ok: bool) -> None:
        key = f"{provider}|{'ok' if ok else 'failed'}"
        self._model_latencies.setdefault(key, []).append(float(latency_ms))
        self._forward("model_latency", float(latency_ms), {"provider": provider, "ok": ok})

    def record_tool_call(self, tool: str, risk: str) -> None:
        key = f"{risk}|{tool}"
        self._tool_calls[key] = self._tool_calls.get(key, 0) + 1
        self._forward("tool_calls", 1.0, {"tool": tool, "risk": risk})

    def record_approval_latency(self, scope: str, seconds: float) -> None:
        self._approval_latencies.setdefault(scope, []).append(float(seconds))
        self._forward("approval_latency", float(seconds), {"scope": scope})

    def record_cost(self, session_id: str, cost_usd: float) -> None:
        if session_id:
            # Bound cardinality: evict oldest when over cap.
            if len(self._cost_by_session) >= 1000 and session_id not in self._cost_by_session:
                try:
                    self._cost_by_session.pop(next(iter(self._cost_by_session)))
                except StopIteration:
                    pass
            self._cost_by_session[session_id] = self._cost_by_session.get(session_id, 0.0) + float(
                cost_usd
            )
        # No per-session OTel label (cardinality explosion); aggregate only.
        self._forward("cost", float(cost_usd), {})

    # -- inspect -----------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """In-memory aggregates (for tests and a future debug endpoint)."""

        def _stats(values: list[float]) -> dict[str, Any]:
            if not values:
                return {"count": 0}
            ordered = sorted(values)
            return {
                "count": len(values),
                "sum": sum(values),
                "avg": sum(values) / len(values),
                "p50": ordered[len(values) // 2],
                "max": ordered[-1],
            }

        return {
            "task_duration_seconds": {
                state: _stats(vals) for state, vals in self._task_durations.items()
            },
            "model_latency_ms": {key: _stats(vals) for key, vals in self._model_latencies.items()},
            "tool_calls": dict(self._tool_calls),
            "approval_latency_seconds": {
                scope: _stats(vals) for scope, vals in self._approval_latencies.items()
            },
            "cost_by_session": dict(self._cost_by_session),
        }


@dataclass
class Telemetry:
    enabled: bool = False
    endpoint: str | None = None
    metrics: Metrics = field(default_factory=Metrics)
    warnings: list[str] = field(default_factory=list)


_current = Telemetry()


def get_metrics() -> Metrics:
    """Process-wide metrics handle (in-memory always, OTel mirrors when enabled)."""
    return _current.metrics


def elapsed_seconds(start: Any, end: Any) -> float | None:
    """Best-effort duration between two datetimes (None-safe, tz-tolerant)."""
    try:
        if start is None or end is None:
            return None
        try:
            return float((end - start).total_seconds())
        except TypeError:
            # Mixed naive/aware datetimes (SQLite round-trips): compare naive.
            start_naive = start.replace(tzinfo=None) if start.tzinfo else start
            end_naive = end.replace(tzinfo=None) if end.tzinfo else end
            return float((end_naive - start_naive).total_seconds())
    except Exception:
        return None


def current_telemetry() -> Telemetry:
    return _current


def setup_telemetry(settings: Any | None = None, app: Any | None = None) -> Telemetry:
    """Enable OTel when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set; else no-op.

    Unset endpoint ⇒ fully disabled, zero overhead, no OTel imports. Set
    endpoint but missing ``telemetry`` extra ⇒ stays disabled with a warning
    (recorded on the handle, never raised — a broken telemetry config must
    never take down the API server).
    """
    global _current
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or (
        str(getattr(settings, "otel_exporter_otlp_endpoint", "") or "") or None
    )
    if not endpoint:
        _current = Telemetry(enabled=False, endpoint=None)
        return _current
    try:
        metrics = _wire_otel(endpoint, app)
    except Exception as exc:
        _current = Telemetry(
            enabled=False,
            endpoint=endpoint,
            warnings=[f"telemetry disabled: {type(exc).__name__}: {exc}"],
        )
        return _current
    _current = Telemetry(enabled=True, endpoint=endpoint, metrics=metrics)
    return _current


def _wire_otel(endpoint: str, app: Any | None) -> Metrics:
    """Build OTel-backed Metrics + auto-instrumentation (raises when unavailable)."""
    try:
        from opentelemetry import metrics as _otel_metrics
        from opentelemetry import trace as _otel_trace
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import (
            PeriodicExportingMetricReader,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
        )
    except ImportError as exc:
        raise TelemetryError(
            "OTEL_EXPORTER_OTLP_ENDPOINT is set but the 'telemetry' extra is "
            f"missing (pip install agent-system[telemetry]): {exc}"
        ) from exc

    resource = Resource.create({"service.name": "bob-agent"})
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint)))
    _otel_trace.set_tracer_provider(tracer_provider)

    reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint))
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    _otel_metrics.set_meter_provider(meter_provider)
    meter = _otel_metrics.get_meter("bob-agent")

    metrics = Metrics(
        _otel={
            "task_duration": meter.create_histogram(
                "bob.task.duration", unit="s", description="Task duration by terminal state"
            ),
            "model_latency": meter.create_histogram(
                "bob.model.latency", unit="ms", description="Model call latency by provider"
            ),
            "tool_calls": meter.create_counter(
                "bob.tool.calls", description="Tool calls by risk tier"
            ),
            "approval_latency": meter.create_histogram(
                "bob.approval.latency",
                unit="s",
                description="Approval decision latency by scope",
            ),
            "cost": meter.create_histogram(
                "bob.cost.usd", unit="USD", description="Model cost per session"
            ),
        }
    )
    if app is not None:
        _instrument_app(app)
    return metrics


def _instrument_app(app: Any) -> list[str]:
    """Best-effort FastAPI + SQLAlchemy + httpx auto-instrumentation."""
    warnings: list[str] = []
    try:
        from opentelemetry.instrumentation.fastapi import (
            FastAPIInstrumentor,
        )

        FastAPIInstrumentor.instrument_app(app)
    except Exception as exc:
        warnings.append(f"fastapi instrumentation skipped: {exc}")
    try:
        from opentelemetry.instrumentation.sqlalchemy import (
            SQLAlchemyInstrumentor,
        )

        SQLAlchemyInstrumentor().instrument()
    except Exception as exc:
        warnings.append(f"sqlalchemy instrumentation skipped: {exc}")
    try:
        from opentelemetry.instrumentation.httpx import (
            HTTPXClientInstrumentor,
        )

        HTTPXClientInstrumentor().instrument()
    except Exception as exc:
        warnings.append(f"httpx instrumentation skipped: {exc}")
    return warnings


__all__ = [
    "Metrics",
    "Telemetry",
    "TelemetryError",
    "current_telemetry",
    "elapsed_seconds",
    "get_metrics",
    "setup_telemetry",
]
