"""Insight generation (v3.1 Phase 17, §15 feature note).

Insights derive ONLY from canonical events (the events table) — never
fabricated, never sourced from private tables. All content passes the secret
scrubber before persistence. Anomalies carry suggested actions.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from agent_system.domain import ids
from agent_system.domain.events import Event
from agent_system.infra.db import session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import EventRow, Insight
from agent_system.services.memory import scrub_text

VALID_TYPES = ("daily", "weekly", "anomaly")


class InsightError(ValueError):
    pass


class InsightGenerator:
    def __init__(self, factory: Any, bus: EventBus | None = None) -> None:
        self._factory = factory
        self._bus = bus

    def generate(self, insight_type: str = "daily") -> dict[str, Any]:
        if insight_type not in VALID_TYPES:
            raise InsightError(f"invalid insight type: {insight_type}")
        if insight_type == "anomaly":
            return self._generate_anomaly()
        findings, content = self._summarize(insight_type)
        insight_id = ids.new_insight_id()
        with session_scope(self._factory) as db:
            db.add(
                Insight(
                    id=insight_id,
                    insight_type=insight_type,
                    content_html=content,
                    key_findings_json=findings,
                )
            )
            self._emit(db, insight_id, insight_type)
        return {
            "insight_id": insight_id,
            "insight_type": insight_type,
            "findings": findings,
            "content": content,
        }

    # -- internals -----------------------------------------------------------

    def _summarize(self, insight_type: str) -> tuple[list[dict[str, Any]], str]:
        """Aggregate canonical events into findings. Zero events -> honest
        empty insight (never fabricated numbers)."""
        with session_scope(self._factory) as db:
            rows = db.query(EventRow).order_by(EventRow.sequence.desc()).limit(5000).all()
        if not rows:
            return (
                [],
                "<p>No events recorded yet — nothing to summarize.</p>",
            )
        type_counts = Counter(r.type for r in rows)
        task_rows = [r for r in rows if r.type == "task.completed"]
        fail_rows = [r for r in rows if r.type == "task.failed"]
        approval_rows = [r for r in rows if r.type.startswith("approval.")]
        cost_rows = [r for r in rows if r.type == "cost.recorded"]

        total_cost = sum(
            float(r.payload.get("cost_usd") or 0) for r in cost_rows if isinstance(r.payload, dict)
        )
        findings: list[dict[str, Any]] = [
            {"finding": f"{sum(type_counts.values())} events analyzed", "kind": "volume"},
            {
                "finding": f"{len(task_rows)} tasks completed, {len(fail_rows)} failed",
                "kind": "tasks",
            },
        ]
        if approval_rows:
            findings.append(
                {"finding": f"{len(approval_rows)} approval events", "kind": "approvals"}
            )
        if cost_rows:
            findings.append(
                {
                    "finding": f"recorded cost ${total_cost:.2f} across {len(cost_rows)} entries",
                    "kind": "cost",
                }
            )
        top = ", ".join(f"{t} ({n})" for t, n in type_counts.most_common(5))
        content = (
            f"<p>Analyzed {sum(type_counts.values())} canonical events. "
            f"Tasks: {len(task_rows)} completed / {len(fail_rows)} failed. "
            f"Top event types: {scrub_text(top)}.</p>"
        )
        return findings, content

    def _generate_anomaly(self) -> dict[str, Any]:
        """Detect anomalies purely from event aggregates + suggest actions."""
        with session_scope(self._factory) as db:
            rows = db.query(EventRow).order_by(EventRow.sequence.desc()).limit(5000).all()
        findings: list[dict[str, Any]] = []
        content_parts: list[str] = []

        fail_events = [r for r in rows if r.type == "task.failed"]
        complete_events = [r for r in rows if r.type == "task.completed"]
        if complete_events or fail_events:
            fail_rate = len(fail_events) / max(1, len(fail_events) + len(complete_events))
            if fail_rate > 0.3:
                findings.append(
                    {
                        "finding": f"task failure rate {fail_rate:.0%} exceeds 30%",
                        "kind": "failure_rate",
                        "suggested_action": "Review recovery patterns; check provider health",
                    }
                )
        recovery_events = [r for r in rows if r.type.startswith("recovery.")]
        if len(recovery_events) >= 5:
            findings.append(
                {
                    "finding": f"{len(recovery_events)} recovery events in recent history",
                    "kind": "instability",
                    "suggested_action": "Inspect agent leases and worker health",
                }
            )
        expired = [r for r in rows if r.type == "approval.expired"]
        if len(expired) >= 3:
            findings.append(
                {
                    "finding": f"{len(expired)} approvals expired without decision",
                    "kind": "approvals",
                    "suggested_action": "Review approval queue responsiveness",
                }
            )
        if not findings:
            findings.append({"finding": "no anomalies detected", "kind": "clean"})
        content_parts.append(
            "<ul>" + "".join(f"<li>{scrub_text(f['finding'])}</li>" for f in findings) + "</ul>"
        )
        insight_id = ids.new_insight_id()
        with session_scope(self._factory) as db:
            db.add(
                Insight(
                    id=insight_id,
                    insight_type="anomaly",
                    content_html="".join(content_parts),
                    key_findings_json=findings,
                )
            )
            self._emit(db, insight_id, "anomaly")
        return {
            "insight_id": insight_id,
            "insight_type": "anomaly",
            "findings": findings,
            "content": "".join(content_parts),
        }

    def _emit(self, db: Any, insight_id: str, insight_type: str) -> None:
        if self._bus is not None:
            self._bus.emit(
                Event(
                    type="insight.generated",
                    actor="insights",
                    payload={"insight_id": insight_id, "insight_type": insight_type},
                ),
                db,
            )
