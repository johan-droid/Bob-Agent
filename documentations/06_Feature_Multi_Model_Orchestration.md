---
title: Feature — Multi-Model Orchestration
type: feature
project: Agent System
status: spec
updated: 2026-09-06
feature_no: 2
up: "[[00_Index]]"
---

# Feature 2 — Multi-Model Orchestration

> **Source:** Spec v3.0 §4 · **Size:** Medium (Phase 11 in v3.1 plan — [[19_Execution_Plan]])
>
> [!warning] v3.1 Refinements (§19)
> - Use **provider adapters**; never hardcode fictional or unavailable provider model IDs (v3.0's `claude-opus-5` etc. are placeholders to be replaced by configured reality).
> - Model configuration must be **external/configurable**.
> - Implement: `ModelRegistry` · `ProviderRegistry` · `ModelSelector` · `PricingRegistry` · `CostPredictor`.
> - Selection factors: task type · latency · capability · context length · cost · availability · historical reliability.
> - **Every model invocation generates a ModelCall record** ([[24_Canonical_Domain_Model]]); cost accounting provider-aware per [[28_Reliability_Operations]] — unknown usage must not crash execution.

## What it does

Route each task/subtask to the most cost-effective or capable LLM. "Summarize logs" → cheap model. "Complex reasoning" → expensive model. "Code review" → medium. Configurable per agent type.

## Execution

```
core/orchestrator/llm_router_v2.py
├── ModelSelector    (per-task decision: cost vs. capability)
├── ProviderRegistry (Anthropic, OpenAI, free tier)
└── CostPredictor    (estimate tokens before committing)

config/model_selection_rules.json
{
  "code": {
    "primary": "claude-opus-5",
    "fallback": "gpt-4o",
    "budget_tier": "claude-sonnet-5"
  },
  "research": {
    "primary": "claude-sonnet-5",
    "budget_tier": "groq"
  },
  "summary": {
    "primary": "claude-haiku-4-5",
    "budget_tier": "groq"
  }
}
```

## Database

```sql
model_selection_log(
  id, task_id,
  candidate_models,   -- json list
  selected_model,
  decision_reason,
  cost_estimate_usd, actual_cost_usd,
  latency_ms, timestamp
)
```

See [[04_Data_Model]].

## Dashboard UI (localhost:3000/cost)

- Cost Optimizer tab: show per-task model selection and estimated vs. actual cost.
- Budget settings panel: set daily/monthly budget caps, alert thresholds.
- Multi-model selector: per-agent-type toggle between "fastest," "cheapest," "most-capable."

## API

```
GET  /api/models
  -> {available: [...], current_selection: {...}}
PUT  /api/models/{agent_type}/prefer
  -> {mode: "fastest|cheapest|capable", apply_to_all: false}
GET  /api/cost/summary?period=day|week|month
  -> {total_usd, by_model, by_agent_type, by_task}
```

Full reference: [[18_API_Reference]].

## Acceptance (DoD)

- [ ] Cost tracking table shows correct model selection per task.
- [ ] Recommendations engine proposes switches.
- [ ] Model rules hot-reloadable from `config/model_selection_rules.json`.
- [ ] Every invocation creates a `model_calls` row ([[04_Data_Model]]); unknown-cost calls complete without error and are flagged estimated.
- [ ] No hardcoded fictional model IDs — registry driven by configuration.

## Non-goals

- Live token counting across different quantized model variants (v2).
