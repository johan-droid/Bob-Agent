"""Resumable task checkpoints (Ollama Cloud-first runtime, additive).

A checkpoint is written before an emergency fallback so a task survives a
provider change with its context intact: conversation/context reference,
completed steps, the pending step, tool results, the active provider/model
and execution metadata. Nothing else.

Hard rules:

- **No secrets, ever.** The payload passes through
  :func:`agent_system.services.secrets.redact_dict`, which strips values by
  key marker (``api_key``, ``authorization``, ``token``…) *and* by value
  pattern before the row is written. API keys, authorization headers and
  credentials never reach the database.
- **Idempotent.** One row per ``task_id``, upserted. Saving twice updates
  the same checkpoint rather than appending a second one.
- **Never fatal.** Checkpointing is a safety net: a storage failure is
  logged and returns ``None``, it must never break inference.
"""

from __future__ import annotations

import logging
from typing import Any

from agent_system.infra.db import session_scope
from agent_system.infra.models import InferenceCheckpoint

logger = logging.getLogger(__name__)

STATUS_OPEN = "open"
STATUS_RESUMED = "resumed"

#: Payload keys retained in a checkpoint. Anything else is dropped — a
#: checkpoint carries only what is needed to resume.
CHECKPOINT_KEYS = (
    "task_state",
    "context_ref",
    "completed_steps",
    "pending_step",
    "tool_results",
    "active_provider",
    "active_model",
    "execution",
)


def build_checkpoint_payload(
    *,
    task_state: str = "",
    context_ref: dict[str, Any] | None = None,
    completed_steps: list[Any] | None = None,
    pending_step: str | None = None,
    tool_results: list[Any] | None = None,
    active_provider: str = "",
    active_model: str = "",
    execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a secret-free checkpoint payload (pure transform).

    Unknown keys never enter the payload; nested values are redacted by key
    marker and by value pattern.
    """
    from agent_system.services.secrets import redact_dict

    raw: dict[str, Any] = {
        "task_state": str(task_state or ""),
        "context_ref": dict(context_ref or {}),
        "completed_steps": list(completed_steps or []),
        "pending_step": pending_step,
        "tool_results": list(tool_results or []),
        "active_provider": str(active_provider or ""),
        "active_model": str(active_model or ""),
        "execution": dict(execution or {}),
    }
    redacted = redact_dict(raw)
    return {key: redacted.get(key) for key in CHECKPOINT_KEYS}


def save_checkpoint(
    factory: Any,
    *,
    task_id: str | None,
    session_id: str | None = None,
    provider: str,
    model_id: str,
    error_kind: str = "UNKNOWN",
    payload: dict[str, Any] | None = None,
    step_index: int = 0,
    status: str = STATUS_OPEN,
) -> str | None:
    """Idempotently upsert the checkpoint for ``task_id``.

    Returns the row id, or ``None`` when there is nothing to persist or the
    write failed (checkpointing is best-effort and never raises).
    """
    if factory is None or not task_id:
        return None
    try:
        from agent_system.domain import ids
        from agent_system.domain.events import utcnow
        from agent_system.services.secrets import redact_dict

        safe_payload = payload if payload is not None else build_checkpoint_payload()
        safe_payload = redact_dict(dict(safe_payload))
        now = utcnow()
        with session_scope(factory) as db:
            row = (
                db.query(InferenceCheckpoint)
                .filter(InferenceCheckpoint.task_id == str(task_id))
                .one_or_none()
            )
            if row is None:
                row = InferenceCheckpoint(
                    id=ids.new_id("ckpt"),
                    task_id=str(task_id),
                    session_id=str(session_id) if session_id else None,
                    created_at=now,
                )
                db.add(row)
            row.session_id = str(session_id) if session_id else row.session_id
            row.step_index = int(step_index or 0)
            row.status = str(status)
            row.provider = str(provider or "")
            row.model_id = str(model_id or "")
            row.error_kind = str(error_kind or "UNKNOWN")
            row.payload_json = safe_payload
            row.updated_at = now
            db.flush()
            return str(row.id)
    except Exception as exc:  # pragma: no cover - safety net
        logger.warning("checkpoint.save_failed task_id=%s error=%s", task_id, exc)
        return None


def load_checkpoint(factory: Any, task_id: str | None) -> dict[str, Any] | None:
    """Load the checkpoint for a task (``None`` when absent/unreadable)."""
    if factory is None or not task_id:
        return None
    try:
        with session_scope(factory) as db:
            row = (
                db.query(InferenceCheckpoint)
                .filter(InferenceCheckpoint.task_id == str(task_id))
                .one_or_none()
            )
            if row is None:
                return None
            return {
                "id": row.id,
                "task_id": row.task_id,
                "session_id": row.session_id,
                "step_index": row.step_index,
                "status": row.status,
                "provider": row.provider,
                "model_id": row.model_id,
                "error_kind": row.error_kind,
                "payload": dict(row.payload_json or {}),
                "updated_at": row.updated_at,
            }
    except Exception as exc:  # pragma: no cover - safety net
        logger.warning("checkpoint.load_failed task_id=%s error=%s", task_id, exc)
        return None


def mark_checkpoint_resumed(
    factory: Any,
    task_id: str | None,
    *,
    provider: str | None = None,
    model_id: str | None = None,
) -> bool:
    """Mark a checkpoint as resumed (idempotent). Returns True when updated."""
    if factory is None or not task_id:
        return False
    try:
        from agent_system.domain.events import utcnow

        with session_scope(factory) as db:
            row = (
                db.query(InferenceCheckpoint)
                .filter(InferenceCheckpoint.task_id == str(task_id))
                .one_or_none()
            )
            if row is None:
                return False
            row.status = STATUS_RESUMED
            row.resumed_at = utcnow()
            row.updated_at = row.resumed_at
            if provider:
                row.provider = str(provider)
            if model_id:
                row.model_id = str(model_id)
            return True
    except Exception as exc:  # pragma: no cover - safety net
        logger.warning("checkpoint.resume_failed task_id=%s error=%s", task_id, exc)
        return False


__all__ = [
    "CHECKPOINT_KEYS",
    "STATUS_OPEN",
    "STATUS_RESUMED",
    "build_checkpoint_payload",
    "load_checkpoint",
    "mark_checkpoint_resumed",
    "save_checkpoint",
]
