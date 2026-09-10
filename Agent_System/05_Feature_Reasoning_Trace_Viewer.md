---
title: Feature — Agent Reasoning Trace Viewer & Live Reasoning Visualization
type: feature
project: Agent System
status: spec
updated: 2026-09-06
feature_no: 1 + 11
up: "[[00_Index]]"
---

# Features 1 & 11 — Decision & Execution Trace (Agent Reasoning Trace Viewer)

> **Source:** Spec v3.0 §4 (Features 1 & 11) · **Size:** Medium
>
> [!warning] v3.1 Refinement (§18) — Consolidates Features 1 & 11
> Canonical name: **Decision & Execution Trace**. Do NOT attempt to expose private hidden chain-of-thought. Capture **auditable** information only:
> task goal · plan summary · decision summary · tool calls & results · model metadata · state transitions · approvals · failures · recovery decisions · final action.
>
> Token-level data is **optional and provider-dependent** — never claim to expose private internal reasoning the model/provider does not provide. The trace UI renders canonical events ([[25_Event_System]]); secrets redacted per [[27_Security_Permissions]].

## What it does

Real-time visualization of the agent's internal decision-making as it unfolds. User sees every token the LLM generates, organized into a decision tree, with branching showing when the agent considers alternatives.

Feature 11 deepens this with animation and interaction: reasoning tree rendered in real time as the agent thinks, with decision branches, explored paths, and choice rationale.

## Execution

```
core/features/reasoning_tracer.py
├── TokenStreamCapture   (intercepts LLM output token by token)
├── ReasoningTreeBuilder (parses tokens into a tree structure)
└── ReasoningRenderer    (converts tree to JSON for frontend visualization)

web/components/ReasoningTree.tsx
├── D3 tree layout + animated transitions
├── Real-time node injection as tokens arrive
└── Interactive node inspection (click to see full token sequence for that branch)
```

## Database

```sql
reasoning_traces(
  id, session_id, agent_id, timestamp,
  tokens_json,         -- list of {token, logprob, timestamp}
  decision_tree_json,  -- hierarchical decision nodes
  final_action,
  latency_ms
)
```

See [[04_Data_Model]].

## Backend behavior (Feature 11)

- Emit reasoning events via WebSocket as the LLM generates tokens.
- Parser detects reasoning markers (e.g., "Let me think..." or structured CoT format).
- Convert to tree nodes on the fly.

## Animation flow

- New reasoning step arrives via WebSocket: `{type: "decision", branch_id, text, confidence}`.
- React adds node to tree, animates entrance (fade + slide).
- User clicks node: sidebar shows token sequence for that branch, logprobs, time spent.
- User can "rewind" to a decision point and ask "what if you'd taken the other path?"

## Dashboard UI (localhost:3000/reasoning)

- Left sidebar: scrollable timeline of reasoning steps, each timestamped.
- Center: animated tree visualization (D3.js or Mermaid) showing decision branches in real time.
- Right sidebar: cost estimate for the current branch, token count, LLM model/temperature.
- Nodes colored by confidence (green = high, yellow = medium, red = low).
- Branch labels show decision criteria (e.g., "research needed" vs. "proceed").
- Playback controls: pause, rewind, fast-forward through thinking steps.

## API

```
WS /ws/reasoning/{session_id}
  -> {type: "token", content: "...", logprob: -0.5, timestamp: ...}
  -> {type: "decision", from_node: "...", options: [...], chosen: "..."}
GET /api/reasoning/{session_id}
  -> full reasoning trace after completion
```

Full reference: [[18_API_Reference]].

## Acceptance (DoD)

- [ ] Decision/execution trace renders in real time from canonical events ([[25_Event_System]]).
- [ ] User can pause and inspect decision branches.
- [ ] Auditable items captured: goal, plan/decision summaries, tool calls, state transitions, approvals, recovery decisions.
- [ ] Token-level data stored when the provider exposes it (optional); UI never claims private reasoning.

## Non-goals

- Exposing private hidden chain-of-thought (prohibited by v3.1 §18).
- Real-time token-level inference-cost optimization (v2 feature). This phase: visualization only.
- Manipulating the reasoning tree (e.g., forcing a different branch) — v2 feature.
