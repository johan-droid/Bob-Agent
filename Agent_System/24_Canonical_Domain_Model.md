---
title: Canonical Domain Model
type: spec
project: Agent System
status: locked
updated: 2026-09-06
up: "[[00_Index]]"
---

# Canonical Domain Model (v3.1 §5, §35)

> One canonical domain model shared by the whole system. No subsystem may invent incompatible identifiers for the same underlying object.

## Core Entities

| Entity | Identifier | Purpose |
| --- | --- | --- |
| Session | `session_id` | A user goal being pursued end-to-end |
| Task | `task_id` | A unit of work in the supervisor's DAG |
| AgentRun | `agent_run_id` | One execution of an agent on a task |
| Agent | (registry entry) | Agent type/instance definition |
| Event | `event_id` | Canonical state transition (see [[25_Event_System]]) |
| ToolCall | `tool_call_id` | One tool invocation by an agent |
| ModelCall | `model_call_id` | One LLM invocation (see [[06_Feature_Multi_Model_Orchestration]]) |
| Approval | `approval_id` | Permission-gate decision record (see [[27_Security_Permissions]]) |
| Workspace | `workspace_id` | Sandboxed coding workspace |
| Artifact | `artifact_id` | Generated file/document |
| Memory | `memory_id` | Memory entry (see [[28_Reliability_Operations]] §Memory) |
| Recording | `recording_id` | Behavior recording for replay (see [[09_Feature_Behavior_Recording]]) |
| Recipe | `recipe_id` | Versioned workflow (see [[13_Feature_Recipe_Library]]) |
| CostRecord | (in ModelCall/Task) | Provider-aware cost accounting (see [[10_Feature_Cost_Optimizer]]) |
| QAReport | `qa_report_id` | Structured test report (see [[12_Feature_Autonomous_QA]]) |
| Insight | `insight_id` | Derived analytics (see [[15_Feature_Insight_Generation]]) |

## ID Conventions

- Prefix + ULID: `ses_…`, `task_…`, `run_…`, `evt_…`, `approval_…`, `ws_…`, etc.
- All timestamps UTC ISO-8601. All JSON validated with Pydantic at boundaries.
- Events carry the full identifier context: `session_id`, `task_id`, `agent_run_id` — this is also the tracing key set for logs (see [[28_Reliability_Operations]]).

## Layered Architecture

```
API
 ↓
Application Services
 ↓
Domain
 ↓
Infrastructure
```

Forbidden dependencies:

- Frontend → database
- Agent → raw SQL
- Agent → host Docker socket
- Feature → another feature's private database tables
- UI → direct filesystem mutation
- CLI → duplicated business logic

Enforce via repositories/services/interfaces. The CLI and dashboard consume the same `/api/v1` contracts ([[18_API_Reference]]).

## Relation to v3.0 Tables

The v3.0 relational tables ([[04_Data_Model]]) are the persistence of these entities; where a v3.0 table lacks an entity identifier above (e.g., tool calls, model calls, events), extend the schema via Alembic migration rather than inventing parallel identifiers.
