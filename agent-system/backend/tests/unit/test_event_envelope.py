"""Unit tests — canonical event envelope + redaction (v3.1 §6, §14)."""

from __future__ import annotations

from agent_system.domain.events import EVENT_TYPES, Event, utcnow
from agent_system.infra.event_bus import redact_payload


def test_event_has_envelope_fields() -> None:
    event = Event(type="task.started", session_id="ses_X", task_id="task_X", actor="supervisor")
    assert event.event_id.startswith("evt_")
    assert event.schema_version == 1
    assert event.sequence is None
    assert event.visibility == "user"
    assert event.sensitivity == "normal"
    assert event.timestamp <= utcnow()


def test_with_sequence_returns_copy() -> None:
    event = Event(type="task.started")
    sequenced = event.with_sequence(42)
    assert sequenced.sequence == 42
    assert event.sequence is None  # original untouched
    assert sequenced.event_id == event.event_id


def test_catalog_contains_all_canonical_families() -> None:
    required = {
        "session.created",
        "session.completed",
        "task.created",
        "task.blocked_approval",
        "task.recovering",
        "agent.started",
        "agent.terminated",
        "model.requested",
        "model.completed",
        "model.failed",
        "tool.started",
        "tool.completed",
        "tool.failed",
        "approval.requested",
        "approval.expired",
        "workspace.created",
        "artifact.created",
        "qa.completed",
        "recovery.completed",
        "recipe.started",
        "cost.recorded",
        "insight.generated",
    }
    assert required.issubset(set(EVENT_TYPES))


def test_redact_payload_masks_secret_keys() -> None:
    payload = {
        "query": "hello",
        "api_key": "sk-abc123",
        "Authorization": "Bearer xyz",
        "user_token": "t0k3n",
        "nested_password": "p@ss",
    }
    cleaned = redact_payload(payload)  # type: ignore[arg-type]
    assert cleaned["query"] == "hello"
    assert cleaned["api_key"] == "[REDACTED]"
    assert cleaned["Authorization"] == "[REDACTED]"
    assert cleaned["user_token"] == "[REDACTED]"
    assert cleaned["nested_password"] == "[REDACTED]"
