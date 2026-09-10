---
title: Architecture
type: spec
project: Agent System
status: locked
updated: 2026-09-06
up: "[[00_Index]]"
---

# 2. Architecture Overview

> **Source:** Spec v3.0 §2

## Layer Diagram

```
┌────────────────────────────────────────────────────────────────────────┐
│                           USER-FACING LAYER                           │
│  ┌──────────────┐       ┌────────────────────────────────────────────┐ │
│  │     CLI      │       │     Web Dashboard (Next.js, localhost)      │ │
│  │  (Typer/Rich)│       │  Chat | Reasoning | Kanban | Workspace      │ │
│  │              │       │  Vault | Outputs | Schedule | Approvals     │ │
│  │              │       │  Templates | Cost | Recipes | Audit         │ │
│  └──────┬───────┘       └──────────────┬──────────────────────────────┘ │
└─────────┼──────────────────────────────┼────────────────────────────────┘
          │ HTTP/WS                      │ HTTP/WS/SSE
          ▼                              ▼
┌────────────────────────────────────────────────────────────────────────┐
│                  ORCHESTRATOR CORE (FastAPI + Redis)                   │
│ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌────────────────┐ │
│ │ Supervisor   │ │ Task Queue   │ │ LLM Router   │ │ Permission     │ │
│ │ (planner)    │ │ (Redis/RQ)   │ │ (multi-model)│ │ Gate           │ │
│ └──────┬───────┘ └──────┬───────┘ └──────┬───────┘ └────────┬───────┘ │
│        │                 │                │                 │          │
│ ┌──────▼─────────────────▼────────────────▼─────────────────▼───────┐ │
│ │              FEATURE PIPELINE (data flowing through)              │ │
│ │ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐              │ │
│ │ │ Cost         │ │ Error        │ │ Reasoning    │              │ │
│ │ │ Optimizer    │ │ Recovery     │ │ Tracer       │              │ │
│ │ └──────────────┘ └──────────────┘ └──────────────┘              │ │
│ └──────────────────────────────────────────────────────────────────┘ │
│                                                                        │
│ ┌──────────────────────────────────────────────────────────────────┐ │
│ │                     SUBAGENT POOL (isolated processes)             │ │
│ │ CodeAgent | BrowserAgent | DocumentAgent | ResearchAgent |         │ │
│ │ SchedulerAgent | AutopilotAgent | CustomAgent                      │ │
│ └──────┬───────────┬───────────┬───────────┬───────────┬────────────┘ │
│        │           │           │           │           │              │
│ ┌──────▼─┐ ┌──────▼──┐ ┌─────▼───┐ ┌────▼──────┐ ┌───▼──────────┐   │
│ │Coding   │ │Browser  │ │Document │ │Memory     │ │Autopilot     │   │
│ │Workspace│ │Runtime  │ │Gen      │ │System     │ │Runtime       │   │
│ │(Docker) │ │(Play-   │ │(pptx/   │ │(LanceDB + │ │(accessibility│   │
│ │         │ │wright)  │ │pdf/docx)│ │Obsidian)  │ │tree + vision)│   │
│ └─────────┘ └─────────┘ └─────────┘ └───────────┘ └──────────────┘   │
│                                                                        │
│ ┌──────────────────────────────────────────────────────────────────┐ │
│ │ Scheduler | Error Recovery Engine | Insight Generator             │ │
│ └──────────────────────────────────────────────────────────────────┘ │
│                                                                        │
│ ┌──────────────────────────────────────────────────────────────────┐ │
│ │ Audit Log | Session Recorder | Cost Tracker | Behavior Recorder   │ │
│ └──────────────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────────────┘
```

## Component Responsibilities

| Component | Responsibility |
| --- | --- |
| **Supervisor** | Decomposes user goals into a task DAG, assigns agent types, monitors completion |
| **Task Queue** | Redis + RQ; holds pending/running tasks with retry semantics |
| **LLM Router** | Per-task model selection (see [[06_Feature_Multi_Model_Orchestration]]) |
| **Permission Gate** | Intercepts risky actions, routes to approval queue |
| **Feature Pipeline** | Cross-cutting data flow: cost, error recovery, reasoning traces |
| **Subagent Pool** | Isolated OS processes per agent; crash-safe |
| **Audit/SRecorder/Tracker** | Persistence of every action for replay & compliance |

## Isolation Boundaries

| Runtime | Isolation mechanism |
| --- | --- |
| Coding workspace | Docker container per workspace |
| Browser automation | Playwright + Docker (recording enabled) |
| Autopilot | Restricted OS account + accessibility tree + vision |
| Subagents | Process isolation + asyncio |

## Key Data Flows

1. **Goal → Tasks:** CLI/Web → Supervisor → task DAG → Task Queue → Subagent Pool.
2. **Reasoning:** LLM tokens → Reasoning Tracer → WS → Dashboard tree (see [[05_Feature_Reasoning_Trace_Viewer]]).
3. **Approvals:** Agent action → Permission Gate → approval queue → user decision → resume.
4. **Memory:** Results → vector DB (LanceDB) + Obsidian vault notes.

## Related

- Stack details: [[03_Tech_Stack]]
- Data model: [[04_Data_Model]]
