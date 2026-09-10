---
title: Feature — Real-Time Cost Optimizer
type: feature
project: Agent System
status: spec
updated: 2026-09-06
feature_no: 6
up: "[[00_Index]]"
---

# Feature 6 — Real-Time Cost Optimizer

> **Source:** Spec v3.0 §4 · **Size:** Medium
>
> [!warning] v3.1 Refinements (§20)
> - Cost accounting is **provider-aware** — never assume all providers expose identical token metrics. Support input/output/cached tokens, estimated tokens, and unknown usage.
> - **Unknown cost must not crash execution** — mark estimated/unknown values explicitly.
> - Budget levels: 50% / 75% / 90% / 100%. Budget scopes: **per-task · per-session · daily · per-provider**.
> - `cost.recorded` is a canonical event ([[25_Event_System]]); ModelCall records per [[24_Canonical_Domain_Model]].

## What it does

Live cost tracking, per-agent and per-task. Alerts when approaching budget. Recommends model switches or task deferral to stay within budget.

## Execution

```
core/features/cost_optimizer.py
├── CostTracker          (log every LLM call, calculate cost)
├── BudgetMonitor        (check remaining budget)
├── CostAlertManager     (email/push/dashboard alerts)
└── RecommendationEngine (suggest model downgrade or task deferral)
```

## Database

```sql
cost_budget(
  id,
  period,               -- daily|weekly|monthly
  limit_usd,
  current_period_start,
  alert_threshold_pct   -- alert at 80%
)

-- llm_usage_log (extended):
model_selected, cost_usd, latency_ms, tokens_in, tokens_out
```

See [[04_Data_Model]].

## Flow

1. Every LLM call logs tokens + model to `llm_usage_log`.
2. CostTracker calculates cost in real time.
3. BudgetMonitor checks against `cost_budget` for current period.
4. At thresholds (50%, 75%, 90%), trigger alerts.
5. RecommendationEngine suggests: "Use Claude Haiku for summaries instead of Opus" or "Defer non-urgent tasks until tomorrow."
6. User can approve suggestions; optimizer applies model selection changes.

## Dashboard UI (localhost:3000/cost)

- Top banner: current period cost, budget remaining, % used, projected end-of-period cost.
- Donut chart: cost breakdown by agent type.
- Table: recent LLM calls with model, tokens, cost, latency.
- Recommendations section: clickable suggestions (e.g., "Save $5.42/day by using Haiku for summarization").
- Budget settings: set period, limit, alert thresholds.

## API

```
GET  /api/cost/status
  -> {period, limit, spent, remaining, pct_used, trend}
GET  /api/cost/breakdown?group_by=agent|model|task
  -> cost by dimension
GET  /api/cost/recommendations
  -> [{action, savings_usd, impact_description}]
POST /api/cost/budget
  -> {period, limit_usd, alert_threshold_pct}
```

Full reference: [[18_API_Reference]].

## Acceptance (DoD)

- [ ] Budget alerts fire at 50/75/90/100% thresholds.
- [ ] Per-call cost logged with model + tokens (provider-aware; cached tokens captured where available).
- [ ] Unknown/estimated usage explicitly flagged; execution never crashes on missing cost data.
- [ ] Per-task, per-session, daily, and per-provider budgets enforced.
- [ ] Recommendations generated and clickable.

## Non-goals

- Predictive budget overflow (v2).
