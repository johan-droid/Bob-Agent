"""Phase 15 — Behavior recording + replay (v3.1 §23) unit tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_system.infra.db import make_engine, make_session_factory
from agent_system.infra.models import Base
from agent_system.services.recording import (
    BehaviorRecorder,
    RecordingContext,
    ReplayBlockedError,
    ReplayContext,
    ReplayError,
    ReplayService,
    read_steps,
    recorded_context,
)


@pytest.fixture()
def db_factory(tmp_path: Path) -> Any:
    """Session factory over a real SQLite file DB (restart-safe semantics)."""
    engine = make_engine(f"sqlite:///{tmp_path / 'rec.db'}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    yield factory
    engine.dispose()


@pytest.fixture()
def recordings_dir(tmp_path: Path) -> Path:
    return tmp_path / "recordings"


@pytest.fixture()
def ctx() -> ReplayContext:
    return ReplayContext(workspace_fingerprint="abc123", model_config="gpt-test")


def _record(recordings_dir: Path, db_factory: Any, context: ReplayContext, steps: int = 2) -> str:
    recorder = BehaviorRecorder(recordings_dir)
    rid = recorder.start(db_factory, session_id="ses_test", context=context)
    for i in range(steps):
        recorder.record("tool_call", "fs.write", {"path": f"f{i}.py", "n": i})
    recorder.finish(db_factory)
    return rid


class TestRecorder:
    def test_records_steps_to_jsonl(
        self, recordings_dir: Path, db_factory: Any, ctx: ReplayContext
    ) -> None:
        rid = _record(recordings_dir, db_factory, ctx)
        steps = read_steps(recordings_dir, rid)
        # header + 2 tool calls + footer
        assert [s.kind for s in steps] == [
            "recording_started",
            "tool_call",
            "tool_call",
            "recording_finished",
        ]
        assert steps[1].payload["path"] == "f0.py"
        assert steps[1].index == 1

    def test_recording_row_persisted(
        self, recordings_dir: Path, db_factory: Any, ctx: ReplayContext
    ) -> None:
        rid = _record(recordings_dir, db_factory, ctx, steps=3)
        from agent_system.infra.db import session_scope
        from agent_system.infra.models import BehaviorRecording

        with session_scope(db_factory) as session:
            row = session.get(BehaviorRecording, rid)
            assert row is not None
            assert row.action_count == 5  # header + 3 + footer
            assert row.recording_end is not None
            assert Path(row.action_log_path).exists()

    def test_secrets_never_in_recording(
        self, recordings_dir: Path, db_factory: Any, ctx: ReplayContext
    ) -> None:
        recorder = BehaviorRecorder(recordings_dir)
        rid = recorder.start(db_factory, context=ctx)
        recorder.record(
            "tool_call",
            "net.fetch",
            {
                "api_key": "sk-abcdefghijklmnop1234",
                "url": "https://x",
                "auth_token": "ghp_" + "a" * 36,
            },
        )
        recorder.finish(db_factory)
        raw = (recordings_dir / f"{rid}.jsonl").read_text()
        assert "sk-abcdefghijklmnop1234" not in raw
        assert "ghp_" + "a" * 36 not in raw
        assert "[REDACTED]" in raw

    def test_double_start_rejected(
        self, recordings_dir: Path, db_factory: Any, ctx: ReplayContext
    ) -> None:
        recorder = BehaviorRecorder(recordings_dir)
        recorder.start(db_factory, context=ctx)
        with pytest.raises(ReplayError):
            recorder.start(db_factory, context=ctx)
        recorder.finish(db_factory)

    def test_record_without_start_rejected(self, recordings_dir: Path) -> None:
        with pytest.raises(ReplayError):
            BehaviorRecorder(recordings_dir).record("tool_call", "x", {})

    def test_recording_context_manager(
        self, recordings_dir: Path, db_factory: Any, ctx: ReplayContext
    ) -> None:
        with RecordingContext(BehaviorRecorder(recordings_dir), db_factory) as rc:
            assert rc.recording_id is not None
            rc.input({"goal": "test"})
            rc.output({"ok": True})
        steps = read_steps(recordings_dir, rc.recording_id or "")
        assert {s.kind for s in steps} >= {"input", "output"}

    def test_roundtrip_context(
        self, recordings_dir: Path, db_factory: Any, ctx: ReplayContext
    ) -> None:
        rid = _record(recordings_dir, db_factory, ctx)
        assert recorded_context(recordings_dir, rid).as_dict() == ctx.as_dict()


class TestReplayFingerprint:
    def test_match_detection(self, ctx: ReplayContext) -> None:
        same = ReplayContext(workspace_fingerprint="abc123", model_config="gpt-test")
        assert ctx.diff(same) == {}
        other = ReplayContext(workspace_fingerprint="different", model_config="gpt-test")
        assert "workspace_fingerprint" in ctx.diff(other)

    def test_inspect_reports_mismatch_but_allows(
        self, recordings_dir: Path, db_factory: Any, ctx: ReplayContext
    ) -> None:
        rid = _record(recordings_dir, db_factory, ctx)
        service = ReplayService(recordings_dir)
        result = service.replay(rid, "INSPECT", ReplayContext(workspace_fingerprint="changed"))
        assert result.allowed is True
        assert result.fingerprint_match is False
        assert "workspace_fingerprint" in result.fingerprint_diff

    def test_simulate_no_side_effects(
        self, recordings_dir: Path, db_factory: Any, ctx: ReplayContext
    ) -> None:
        rid = _record(recordings_dir, db_factory, ctx)
        calls: list[str] = []

        def handler(payload: dict[str, Any], _ctx: dict[str, Any]) -> dict[str, Any]:
            calls.append(payload["path"])
            return {"dry": True}

        service = ReplayService(recordings_dir)
        result = service.replay(
            rid,
            "SIMULATE",
            ctx,
            handlers={"fs.write": handler},
        )
        assert result.side_effects == 0
        assert result.allowed is True
        # dry-run handler executed but flagged
        assert calls  # simulation ran through dry handlers


class TestReplaySafety:
    """The three-mode boundary tests (Phase 15 acceptance)."""

    def test_reexecute_without_approval_blocked(
        self, recordings_dir: Path, db_factory: Any, ctx: ReplayContext
    ) -> None:
        rid = _record(recordings_dir, db_factory, ctx)
        service = ReplayService(recordings_dir)
        with pytest.raises(ReplayBlockedError, match="approval"):
            service.replay(rid, "APPROVED_REEXECUTE", ctx, approval_id=None)

    def test_reexecute_fingerprint_mismatch_blocked(
        self, recordings_dir: Path, db_factory: Any, ctx: ReplayContext
    ) -> None:
        rid = _record(recordings_dir, db_factory, ctx)
        service = ReplayService(recordings_dir)
        with pytest.raises(ReplayBlockedError, match="fingerprint"):
            service.replay(
                rid,
                "APPROVED_REEXECUTE",
                ReplayContext(workspace_fingerprint="changed"),
                approval_id="approval_x",
            )

    def test_reexecute_with_match_and_approval_executes(
        self, recordings_dir: Path, db_factory: Any, ctx: ReplayContext
    ) -> None:
        rid = _record(recordings_dir, db_factory, ctx, steps=2)
        executed: list[str] = []

        def handler(payload: dict[str, Any], _ctx: dict[str, Any]) -> dict[str, Any]:
            executed.append(payload["path"])
            return {"wrote": payload["path"]}

        service = ReplayService(recordings_dir)
        result = service.replay(
            rid,
            "APPROVED_REEXECUTE",
            ctx,
            approval_id="approval_ok",
            handlers={"fs.write": handler},
        )
        assert result.allowed is True
        assert result.side_effects == 2
        assert result.fingerprint_match is True
        assert len(executed) == 2

    def test_invalid_mode_rejected(
        self, recordings_dir: Path, db_factory: Any, ctx: ReplayContext
    ) -> None:
        rid = _record(recordings_dir, db_factory, ctx)
        with pytest.raises(ReplayError, match="mode"):
            ReplayService(recordings_dir).replay(rid, "DESTROY", ctx)

    def test_missing_recording_rejected(self, recordings_dir: Path, ctx: ReplayContext) -> None:
        with pytest.raises(ReplayError, match="not found"):
            ReplayService(recordings_dir).replay("rec_none", "INSPECT", ctx)
