"""Canonical identifiers for all entities (v3.1 §5).

Every entity gets a stable prefixed-ULID identifier. No subsystem may invent
incompatible identifiers for the same underlying object.
"""

from __future__ import annotations

from ulid import ULID


def new_id(prefix: str) -> str:
    """Generate a new prefixed ULID, e.g. ``ses_01J9Z8G4K0...``."""
    return f"{prefix}_{ULID()}"


def new_session_id() -> str:
    return new_id("ses")


def new_task_id() -> str:
    return new_id("task")


def new_agent_run_id() -> str:
    return new_id("run")


def new_event_id() -> str:
    return new_id("evt")


def new_tool_call_id() -> str:
    return new_id("toolcall")


def new_model_call_id() -> str:
    return new_id("modelcall")


def new_approval_id() -> str:
    return new_id("approval")


def new_workspace_id() -> str:
    return new_id("ws")


def new_artifact_id() -> str:
    return new_id("art")


def new_memory_id() -> str:
    return new_id("mem")


def new_recording_id() -> str:
    return new_id("rec")


def new_recipe_id() -> str:
    return new_id("recipe")


def new_batch_id() -> str:
    return new_id("batch")


def new_qa_report_id() -> str:
    return new_id("qa")


def new_insight_id() -> str:
    return new_id("insight")
