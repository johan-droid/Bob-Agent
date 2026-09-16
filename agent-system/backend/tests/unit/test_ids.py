"""Unit tests — canonical identifiers."""

from __future__ import annotations

import re

from agent_system.domain import ids

EXPECTED_PREFIXES = {
    "new_session_id": "ses",
    "new_task_id": "task",
    "new_agent_run_id": "run",
    "new_event_id": "evt",
    "new_tool_call_id": "toolcall",
    "new_model_call_id": "modelcall",
    "new_approval_id": "approval",
    "new_workspace_id": "ws",
    "new_artifact_id": "art",
    "new_memory_id": "mem",
    "new_recording_id": "rec",
    "new_recipe_id": "recipe",
    "new_batch_id": "batch",
    "new_qa_report_id": "qa",
    "new_insight_id": "insight",
}


def test_all_id_factories_use_prefixed_ulids() -> None:
    pattern = re.compile(r"^[a-z]+_[0-9A-HJKMNP-TV-Z]{26}$")
    for factory_name, prefix in EXPECTED_PREFIXES.items():
        value = getattr(ids, factory_name)()
        assert value.startswith(f"{prefix}_"), value
        assert pattern.match(value), f"{factory_name} produced non-ULID: {value}"


def test_ids_are_unique() -> None:
    seen = {ids.new_task_id() for _ in range(1000)}
    assert len(seen) == 1000
