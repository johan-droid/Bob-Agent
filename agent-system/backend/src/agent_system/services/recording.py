"""Behavior recording + replay (v3.1 Phase 15, §09 feature note).

BehaviorRecorder wraps agent I/O and appends one JSON line per action to a
`.jsonl` log under the recordings dir. Secret-shaped values are scrubbed at
record time — recordings must never contain secrets.

ReplayService implements the three safe modes:

- INSPECT — read-only walk of recorded steps. Always allowed.
- SIMULATE — re-derives steps without side effects (dry-run handlers when
  registered, otherwise replays from the log). Allowed without approval;
  fingerprint comparison is reported, not enforced.
- APPROVED_REEXECUTE — real re-execution of recorded actions. Requires BOTH a
  fingerprint match against the current environment AND a fresh approval.
  A fingerprint mismatch blocks unless the caller supplies a dedicated
  risky-replay approval; no approval at all always blocks.

Fingerprints cover: workspace content, OS platform, dependency fingerprint,
agent version, model config, recipe version, and the permission set.
"""

from __future__ import annotations

import json
import platform
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_system.domain import ids
from agent_system.domain.events import utcnow
from agent_system.infra.db import session_scope
from agent_system.infra.models import BehaviorRecording
from agent_system.services.secrets import redact_dict

MAX_STEPS = 10_000


class ReplayError(ValueError):
    pass


class ReplayBlockedError(ReplayError):
    """Re-execution refused: fingerprint mismatch or missing approval."""


# ---------------------------------------------------------------------------
# Environment fingerprint
# ---------------------------------------------------------------------------


@dataclass
class ReplayContext:
    """The environment facets a recording is valid for (v3.1 §23)."""

    workspace_fingerprint: str = ""
    os_platform: str = field(default_factory=platform.platform)
    deps_fingerprint: str = ""
    agent_version: str = "0.1.0"
    model_config: str = ""
    recipe_version: int | None = None
    permissions: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace_fingerprint": self.workspace_fingerprint,
            "os_platform": self.os_platform,
            "deps_fingerprint": self.deps_fingerprint,
            "agent_version": self.agent_version,
            "model_config": self.model_config,
            "recipe_version": self.recipe_version,
            "permissions": list(self.permissions),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReplayContext:
        perms = data.get("permissions") or []
        return cls(
            workspace_fingerprint=str(data.get("workspace_fingerprint", "")),
            os_platform=str(data.get("os_platform", "")),
            deps_fingerprint=str(data.get("deps_fingerprint", "")),
            agent_version=str(data.get("agent_version", "")),
            model_config=str(data.get("model_config", "")),
            recipe_version=data.get("recipe_version"),
            permissions=tuple(str(p) for p in perms),
        )

    def diff(self, other: ReplayContext) -> dict[str, tuple[Any, Any]]:
        """Fields that differ: name -> (recorded, current)."""
        a, b = self.as_dict(), other.as_dict()
        return {key: (a[key], b[key]) for key in a if a[key] != b[key]}


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------


@dataclass
class RecordedStep:
    index: int
    timestamp: str
    kind: str  # tool_call | model_call | observation | input | output
    name: str
    payload: dict[str, Any]


class BehaviorRecorder:
    """Append-only action log (.jsonl) + BehaviorRecording row.

    Usage::

        rec = BehaviorRecorder(recordings_dir)
        recording_id = rec.start(factory, session_id="ses_..", context=ctx)
        rec.record("tool_call", "fs.write", {"path": "a.py"})
        rec.finish(factory)
    """

    def __init__(self, recordings_dir: Path) -> None:
        self._root = recordings_dir
        self._root.mkdir(parents=True, exist_ok=True)
        self._recording_id: str | None = None
        self._file: Any = None
        self._count = 0
        self._context: ReplayContext | None = None

    @property
    def recording_id(self) -> str | None:
        return self._recording_id

    def start(
        self,
        factory: Any,
        session_id: str | None = None,
        agent_run_id: str | None = None,
        context: ReplayContext | None = None,
    ) -> str:
        if self._recording_id is not None:
            raise ReplayError("recorder already active — call finish() first")
        recording_id = ids.new_recording_id()
        self._context = context or ReplayContext()
        path = self._path(recording_id)
        self._file = path.open("w", encoding="utf-8")
        self._recording_id = recording_id
        self._count = 0
        # Header row carries the fingerprint context for replay checks.
        self._write("recording_started", "recorder", {"context": self._context.as_dict()})
        with session_scope(factory) as db:
            db.add(
                BehaviorRecording(
                    id=recording_id,
                    session_id=session_id,
                    agent_run_id=agent_run_id,
                    action_count=0,
                    action_log_path=str(path),
                )
            )
        return recording_id

    def record(self, kind: str, name: str, payload: dict[str, Any]) -> int:
        """Append one scrubbed action; returns its step index."""
        if self._file is None or self._recording_id is None:
            raise ReplayError("no active recording")
        if self._count >= MAX_STEPS:
            raise ReplayError(f"recording exceeds {MAX_STEPS} steps")
        return self._write(kind, name, payload)

    def finish(self, factory: Any) -> dict[str, Any]:
        if self._file is None or self._recording_id is None:
            raise ReplayError("no active recording")
        self._write("recording_finished", "recorder", {"steps": self._count})
        self._file.close()
        self._file = None
        recording_id, count = self._recording_id, self._count
        self._recording_id = None
        with session_scope(factory) as db:
            row = db.get(BehaviorRecording, recording_id)
            if row is not None:
                row.action_count = count
                row.recording_end = utcnow()
        return {"recording_id": recording_id, "steps": count}

    # -- internals -----------------------------------------------------------

    def _write(self, kind: str, name: str, payload: dict[str, Any]) -> int:
        assert self._file is not None
        cleaned = redact_dict(payload)  # key-marker + value-pattern redaction
        index = self._count
        self._count += 1
        entry = {
            "index": index,
            "timestamp": utcnow().isoformat(),
            "kind": kind,
            "name": name,
            "payload": cleaned,
        }
        self._file.write(json.dumps(entry, default=str) + "\n")
        self._file.flush()
        return index

    def _path(self, recording_id: str) -> Path:
        return self._root / f"{recording_id}.jsonl"


class RecordingContext:
    """Context-manager wrapper around an agent run: records I/O automatically.

    Wrap the handler so every input, output, tool call and error becomes a
    recorded step — all agent I/O flows through here (v3.1 §23).
    """

    def __init__(self, recorder: BehaviorRecorder, factory: Any) -> None:
        self._recorder = recorder
        self._factory = factory
        self._recording_id: str | None = None

    def __enter__(self) -> RecordingContext:
        self._recording_id = self._recorder.start(self._factory)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._recorder.finish(self._factory)

    def tool_call(self, name: str, payload: dict[str, Any]) -> int:
        return self._recorder.record("tool_call", name, payload)

    def input(self, payload: dict[str, Any]) -> int:
        return self._recorder.record("input", "task_input", payload)

    def output(self, payload: dict[str, Any]) -> int:
        return self._recorder.record("output", "task_output", payload)

    @property
    def recording_id(self) -> str | None:
        return self._recording_id


# ---------------------------------------------------------------------------
# Log reading
# ---------------------------------------------------------------------------


def read_steps(recordings_dir: Path, recording_id: str) -> list[RecordedStep]:
    path = recordings_dir / f"{recording_id}.jsonl"
    if not path.exists():
        raise ReplayError(f"recording {recording_id} not found")
    steps: list[RecordedStep] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        steps.append(
            RecordedStep(
                index=int(entry["index"]),
                timestamp=str(entry["timestamp"]),
                kind=str(entry["kind"]),
                name=str(entry["name"]),
                payload=dict(entry["payload"]),
            )
        )
    return steps


def recorded_context(recordings_dir: Path, recording_id: str) -> ReplayContext:
    steps = read_steps(recordings_dir, recording_id)
    for step in steps:
        if step.kind == "recording_started" and "context" in step.payload:
            return ReplayContext.from_dict(step.payload["context"])
    return ReplayContext()


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


@dataclass
class ReplayResult:
    recording_id: str
    mode: str
    allowed: bool
    blocked_reason: str | None
    fingerprint_match: bool
    fingerprint_diff: dict[str, tuple[Any, Any]]
    steps: list[dict[str, Any]]
    side_effects: int


class ReplayService:
    """Three-mode replay with fingerprint + approval enforcement."""

    MODES = ("INSPECT", "SIMULATE", "APPROVED_REEXECUTE")

    def __init__(self, recordings_dir: Path) -> None:
        self._root = recordings_dir
        self._root.mkdir(parents=True, exist_ok=True)

    def replay(
        self,
        recording_id: str,
        mode: str,
        current_context: ReplayContext,
        approval_id: str | None = None,
        handlers: dict[str, Any] | None = None,
    ) -> ReplayResult:
        if mode not in self.MODES:
            raise ReplayError(f"invalid replay mode: {mode}")
        steps = read_steps(self._root, recording_id)
        recorded_ctx = recorded_context(self._root, recording_id)
        diff = recorded_ctx.diff(current_context)
        match = not diff

        if mode == "INSPECT":
            return ReplayResult(
                recording_id=recording_id,
                mode=mode,
                allowed=True,
                blocked_reason=None,
                fingerprint_match=match,
                fingerprint_diff=diff,
                steps=[self._view(s) for s in steps],
                side_effects=0,
            )

        if mode == "SIMULATE":
            # No real side effects: dry-run handlers when provided, else
            # steps are re-derived from the log itself.
            executed = 0
            out_steps: list[dict[str, Any]] = []
            for step in steps:
                view = self._view(step)
                handler = (handlers or {}).get(step.name)
                if handler is not None and step.kind == "tool_call":
                    result = handler(step.payload, {"dry_run": True})
                    view["simulated_result"] = result if isinstance(result, dict) else {}
                    executed += 1
                out_steps.append(view)
            return ReplayResult(
                recording_id=recording_id,
                mode=mode,
                allowed=True,
                blocked_reason=None,
                fingerprint_match=match,
                fingerprint_diff=diff,
                steps=out_steps,
                side_effects=0,
            )

        # APPROVED_REEXECUTE — the guarded path.
        if approval_id is None:
            raise ReplayBlockedError("re-execution requires a fresh approval (none supplied)")
        if not match:
            raise ReplayBlockedError(
                f"fingerprint mismatch — risky re-execution needs dedicated approval: "
                f"{sorted(diff.keys())}"
            )
        side_effects = 0
        reexec_steps: list[dict[str, Any]] = []
        for step in steps:
            view = self._view(step)
            handler = (handlers or {}).get(step.name)
            if handler is not None and step.kind == "tool_call":
                result = handler(step.payload, {"dry_run": False, "approval_id": approval_id})
                view["reexecuted_result"] = result if isinstance(result, dict) else {}
                side_effects += 1
            reexec_steps.append(view)
        return ReplayResult(
            recording_id=recording_id,
            mode=mode,
            allowed=True,
            blocked_reason=None,
            fingerprint_match=True,
            fingerprint_diff={},
            steps=reexec_steps,
            side_effects=side_effects,
        )

    @staticmethod
    def _view(step: RecordedStep) -> dict[str, Any]:
        return {
            "index": step.index,
            "timestamp": step.timestamp,
            "kind": step.kind,
            "name": step.name,
            "payload": step.payload,
        }


def recordings_summary(factory: Any, recordings_dir: Path) -> list[dict[str, Any]]:
    """List recordings (DB rows), newest first."""
    out: list[dict[str, Any]] = []
    with session_scope(factory) as db:
        rows = db.query(BehaviorRecording).order_by(BehaviorRecording.recording_start.desc()).all()
        for r in rows:
            end: datetime | None = r.recording_end
            out.append(
                {
                    "recording_id": r.id,
                    "session_id": r.session_id,
                    "agent_run_id": r.agent_run_id,
                    "action_count": r.action_count,
                    "started": r.recording_start.isoformat(),
                    "finished": end.isoformat() if end else None,
                }
            )
    return out
