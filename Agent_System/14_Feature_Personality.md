---
title: Feature — Agent Personality & Tone Adaptation
type: feature
project: Agent System
status: spec
updated: 2026-09-06
feature_no: 10
up: "[[00_Index]]"
---

# Feature 10 — Agent Personality & Tone Adaptation

> **Source:** Spec v3.0 §4 · **Size:** Medium (Phase 17 in v3.1 plan — [[19_Execution_Plan]])
>
> [!warning] v3.1 Refinements (§27)
> Personality is a **presentation/behavior layer**. It must NOT silently modify: security policies · permission policies · system safety constraints · resource limits.
> Configuration must be: **versioned · user-visible · persisted · auditable**. Feedback must not silently rewrite core system instructions.

## What it does

Customize how agents communicate (verbose vs. terse, formal vs. casual) and their reasoning style (fast heuristic vs. careful step-by-step). Learned per-agent preferences over time.

## Execution

```
core/agents/personality.py
├── PersonalityConfig        (store per-agent: tone, verbosity, reasoning_style)
├── PersonalityInference     (LLM system prompt injection)
└── FeedbackCollector        (learn from user ratings)
```

## Database

```sql
agent_personalities(
  id, agent_id,
  tone,                     -- "formal", "casual", "terse", "verbose"
  verbosity,                -- 1–10 scale
  reasoning_style,          -- "fast", "careful", "socratic"
  system_prompt_override,
  learned_from_feedback_count,
  updated_at
)

feedback_log(
  id, agent_id, session_id,
  rating,                   -- 1–5 stars
  comment,
  timestamp
)
```

See [[04_Data_Model]].

## Flow

1. User rates an agent's output: "I liked the concise answer, give more of that" (5 stars + comment).
2. FeedbackCollector stores to `feedback_log`.
3. After N feedback items (e.g., 10), PersonalityInference re-analyzes trend: "user prefers terse output."
4. Adjusts `agent_personalities.verbosity` or `tone`.
5. On next agent call, inject adjusted system prompt: "Answer concisely in 1–2 sentences."

## Dashboard UI (localhost:3000/agents)

- Agent settings panel: tone selector (formal/casual/terse/verbose), verbosity slider (1–10), reasoning_style buttons.
- Feedback box after agent output: quick 1–5 star rating + optional comment.
- "Learned preferences" section: show inferred tone/verbosity based on feedback, with confidence score.
- "Reset to Default" button to revert customizations.

## API

```
GET  /api/agents/{id}/personality
  -> {tone, verbosity, reasoning_style, system_prompt_override}
PUT  /api/agents/{id}/personality
  -> update personality config
POST /api/agents/{id}/feedback
  -> {rating, comment}
```

Full reference: [[18_API_Reference]].

## Acceptance (DoD)

- [ ] Feedback collected via star rating.
- [ ] After N ratings, personality config adjusts automatically.
- [ ] System prompt override injected on next agent call.
- [ ] Personality config versioned and auditable; cannot alter security/permission/safety/resource settings.
- [ ] Core system instructions never silently rewritten by feedback.

## Non-goals

- Multi-user personality profiles (v2). Single user, v1.
