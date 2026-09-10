---
title: Feature — Agent Behavior Recording & Replay
type: feature
project: Agent System
status: spec
updated: 2026-09-06
feature_no: 5
up: "[[00_Index]]"
---

# Feature 5 — Agent Behavior Recording & Replay

> **Source:** Spec v3.0 §4 · **Size:** Medium (Phase 15 in v3.1 plan — [[19_Execution_Plan]])
>
> [!warning] v3.1 Refinements (§23)
> Replay modes: **INSPECT · SIMULATE · APPROVED_REEXECUTE**. Never blindly re-execute arbitrary historical actions.
> Before re-execution compare: workspace fingerprint · OS · dependencies · agent version · model configuration · recipe version · permissions.
> Destructive/network/payment actions require explicit approval. Recordings contain redacted data only ([[27_Security_Permissions]]).

## What it does

Record every action an agent takes (API calls, file changes, LLM prompts, decisions). Replay the sequence for debugging or auditing. Essential for understanding why an agent did something unexpected.

## Execution

```
core/audit/behavior_recorder.py
├── ActionCapture    (intercept all agent I/O)
├── ActionSerializer (convert to replay format)
└── ReplayEngine     (re-execute sequence)
```

## Database

```sql
behavior_recordings(
  id, session_id, agent_id,
  recording_start, recording_end,
  action_count,
  action_log_path      -- jsonl file in recordings/
)
```

Action log (`.jsonl`, appended):

```json
{
  "seq": 1,
  "timestamp": "2026-09-06T12:34:56Z",
  "action_type": "llm_call|file_write|shell_exec|decision",
  "details": {},
  "state_before": {},
  "state_after": {},
  "duration_ms": 123
}
```

See [[04_Data_Model]].

## Flow

1. Every subagent wraps its I/O with `RecordingContext`.
2. All actions (LLM calls, file ops, shell commands) are serialized to `.jsonl`.
3. User visits dashboard's "Recording Replay" tab, selects a session.
4. Replay engine step-through: execute each action, show state transition, pause on error or user click.
5. Can skip to a specific step or re-run from a checkpoint.

## Dashboard UI (localhost:3000/replay)

- Session selector: list recent recordings with duration and action count.
- Timeline scrubber: jump to any point in the recording.
- Step-through controls: play, pause, next, prev, 1x/2x/4x speed.
- Side panels:
  - Left: action log with clickable entries.
  - Center: live output/result of current step.
  - Right: agent state (variables, context, memory at current step).
- Diff viewer for file-write actions.

## API

```
GET  /api/recordings/{session_id}
  -> metadata + action log
GET  /api/recordings/{session_id}/replay/step/{seq}
  -> state at that step
POST /api/recordings/{session_id}/replay/resume-from/{seq}
  -> re-execute from checkpoint
```

Full reference: [[18_API_Reference]].

## Acceptance (DoD)

- [ ] A session is recorded, replay engine steps through actions.
- [ ] State transitions are visible step by step.
- [ ] All three replay modes boundary-tested ([[29_Testing_Strategy]] §Replay); re-execution blocked on fingerprint mismatch without approval.

## Non-goals

- Deterministic replay with exact environment restoration (v2).
