---
title: Feature — Workflow Recipe Library
type: feature
project: Agent System
status: spec
updated: 2026-09-06
feature_no: 9
up: "[[00_Index]]"
---

# Feature 9 — Workflow Recipe Library

> **Source:** Spec v3.0 §4 · **Size:** Medium (Phase 16 in v3.1 plan — [[19_Execution_Plan]])
>
> [!warning] v3.1 Refinements (§26)
> Recipes must be **versioned**. Execution uses the canonical Task/Event system ([[24_Canonical_Domain_Model]], [[25_Event_System]]) — never a separate path. Support: DAG dependencies · parameters · validation · execution history · cancellation · failure handling.
> **Recipes must not bypass permission checks.**

## What it does

Compose and save multi-step workflows (sequences of agent calls with dependencies). Reuse with one click. "Automated Blog Publishing Workflow" = research → write → generate image → create PDF → upload. Save, then trigger anytime.

## Execution

```
core/recipes/recipe_manager.py
├── RecipeBuilder   (compose from task DAG)
├── RecipeRegistry  (store, version)
└── RecipeExecutor  (instantiate and run)
```

## Database

```sql
recipes(
  id, name, description,
  task_dag_json,      -- task graph definition
  parameters_json,    -- {param_name: default_value}
  tags_json,
  created_at, executions_count
)
```

See [[04_Data_Model]].

## Recipe JSON structure

```json
{
  "name": "Blog Publishing Pipeline",
  "params": {
    "topic": "string",
    "output_format": ["pdf", "pptx"]
  },
  "tasks": [
    {"id": "t1", "type": "research", "input": {"query": "${topic}"}},
    {"id": "t2", "type": "document", "depends_on": ["t1"], "input": {"outline": "${t1.result}"}},
    {"id": "t3", "type": "browser", "depends_on": ["t2"], "input": {"upload": "${t2.result}"}}
  ]
}
```

## Flow

1. User completes a multi-step workflow manually once (research + write + format).
2. Clicks "Save as Recipe" from the task graph view.
3. System extracts the DAG, prompts for recipe name + params.
4. Stores to `recipes` table.
5. Next time: user goes to Recipes tab, clicks recipe, fills in params, hits "Run Recipe."
6. System instantiates the DAG, substitutes params, submits to Supervisor.
7. Workflow executes exactly like before, but in one click.

## Dashboard UI (localhost:3000/recipes)

- Recipes tab: gallery of saved recipes with descriptions, tags, usage count.
- Recipe detail: show DAG visualization, parameter form, quick-run button.
- "Save as Recipe" button visible in Kanban view after a multi-task workflow completes.
- Recipe execution history: show past runs with parameters and outcomes.

## API

```
GET  /api/recipes
  -> [{name, description, param_count, usage_count}]
POST /api/recipes
  -> {name, description, task_dag, parameters}
POST /api/recipes/{id}/execute
  -> {param_values} -> submits to supervisor, returns session_id
GET  /api/recipes/{id}/executions
  -> past runs
```

Full reference: [[18_API_Reference]].

## Acceptance (DoD)

- [ ] Multi-task workflow saved as recipe from Kanban view.
- [ ] One-click re-run with parameter substitution.
- [ ] Recipes versioned; execution through canonical task/event pipeline.
- [ ] Permission checks enforced inside recipe execution; cancellation and partial failure handled.

## Non-goals

- Recipe marketplace (v3). Local library only, v1.
