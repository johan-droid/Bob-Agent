---
title: Feature — Intelligent Task Batching
type: feature
project: Agent System
status: spec
updated: 2026-09-06
feature_no: 7
up: "[[00_Index]]"
---

# Feature 7 — Intelligent Task Batching

> **Source:** Spec v3.0 §4 · **Size:** Medium
>
> [!warning] v3.1 Refinements (§24)
> Only batch **compatible** tasks. Compatibility considers: agent type · workspace · task type · required permissions · dependencies · resource requirements. Never mix unrelated secrets or contexts.
> Implement: `batch_id` · idempotency · cancellation · partial failure · concurrency limits · per-task result tracking.

## What it does

When multiple similar tasks are queued, the orchestrator automatically groups and parallelizes them, reducing overhead and improving throughput.

## Execution

```
core/orchestrator/task_batcher.py
├── TaskSimilarityScorer (embedding-based grouping)
├── BatchOptimizer       (decide parallelism strategy)
└── BatchExecutor        (run tasks concurrently in worker pool)
```

## Logic

- When ≥3 tasks of same type queued (e.g., "research X," "research Y," "research Z"):
  - Score similarity via embedding distance.
  - If score > threshold, group into a batch.
  - Create a meta-task: `batch_research([...])`.
  - ResearchAgent processes all at once: search for all, fetch all, summarize all, deduplicate insights.
  - Return results as individual task outcomes.

## Database

```sql
task_batches(
  id,
  batch_type,        -- "code_review", "research", etc.
  task_ids_json,     -- list of original task IDs
  member_count,
  created_at, completed_at,
  speedup_factor     -- ratio of sequential vs. batch time
)
```

See [[04_Data_Model]].

## Benefit

If 3 research tasks take 30s each sequentially (90s), batching them into one call with parallel search/fetch reduces to ~35s. **2.5x speedup.**

## Dashboard UI (localhost:3000/kanban)

- When tasks are batched, show them grouped with a "Batch" badge.
- Hover over batch: show member tasks, estimated speedup.
- Toggle "Enable Auto-Batching" in settings.

## API

```
GET /api/tasks/batching-config
  -> {enabled, similarity_threshold, min_batch_size}
PUT /api/tasks/batching-config
  -> update settings
GET /api/tasks/batches?limit=20
  -> recent batches with member counts and speedup metrics
```

Full reference: [[18_API_Reference]].

## Acceptance (DoD)

- [ ] ≥3 similar queued tasks auto-group into a batch.
- [ ] Speedup factor recorded per batch.
- [ ] Batch idempotency, cancellation, and partial-failure handling verified.
- [ ] Incompatible tasks never mixed (permissions/workspace/secret contexts).

## Non-goals

- Cross-agent batching (e.g., code review + research in one batch). Single-type batching only, v1.
