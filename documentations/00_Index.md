---
title: Local Autonomous AI Agent System — Index
type: moc
project: Agent System
spec_version: "3.1"
status: implemented (Phases 0-21)
updated: 2026-09-16
---

# 🤖 Local Autonomous AI Agent System — Index (MOC)

> [!info] Project Hub
> A locally-hosted, persistent, multi-agent AI system. Owner **Ashutosh**.
> **v3.0** = product vision · **v3.1** = engineering contract (2026-09-06).
> Start here, then follow the links below.

## 📌 Quick Links

| Note | Purpose |
| --- | --- |
| **[[99_Master_Build_Plan]]** | ⭐ **START HERE — single consolidated execution doc: v3.0 ⊕ v3.1, all phases, kickoff commands** |
| [[01_Overview]] | System summary, core capabilities, competitive features, non-goals |
| [[02_Architecture]] | Layer diagram, orchestrator core, subagent pool, runtime topology |
| [[03_Tech_Stack]] | Locked stack decisions with rationale |
| [[04_Data_Model]] | All database tables (core + feature tables), SQL schema |
| [[05_Feature_Reasoning_Trace_Viewer]] | Feature 1 & 11 — live reasoning tree |
| [[06_Feature_Multi_Model_Orchestration]] | Feature 2 — per-task LLM routing |
| [[07_Feature_Error_Recovery]] | Feature 3 — self-healing agent pipeline |
| [[08_Feature_Workspace_Templates]] | Feature 4 — snapshot & clone workspaces |
| [[09_Feature_Behavior_Recording]] | Feature 5 — record & replay agent actions |
| [[10_Feature_Cost_Optimizer]] | Feature 6 — budgets, alerts, recommendations |
| [[11_Feature_Task_Batching]] | Feature 7 — intelligent batch grouping |
| [[12_Feature_Autonomous_QA]] | Feature 8 — generated tests & coverage |
| [[13_Feature_Recipe_Library]] | Feature 9 — reusable workflow DAGs |
| [[14_Feature_Personality]] | Feature 10 — tone & verbosity adaptation |
| [[15_Feature_Insight_Generation]] | Feature 12 — scheduled briefings & anomalies |
| [[16_Dashboard_UIUX]] | Full web dashboard spec — design tokens, all 11+ tabs |
| [[17_CLI_Specification]] | `agentctl` commands, TUI outputs, scripting flags |
| [[18_API_Reference]] | REST + WS endpoints grouped by domain |
| [[19_Execution_Plan]] | Phase 0–21 build order with acceptance criteria (v3.1) |
| [[20_Deployment]] | Install, services, env, service management |
| [[21_Progress_Log]] | Living progress & decision log |
| [[22_Decision_Log]] | Architecture decision records (ADRs) |

### v3.1 Engineering Contract

| Note | Purpose |
| --- | --- |
| [[23_Engineering_Contract]] | v3.1 contract: hierarchy, autonomy policy, phase gate, DoD, no-shortcuts |
| [[24_Canonical_Domain_Model]] | Entities, stable identifiers, layered architecture rules |
| [[25_Event_System]] | Event envelope, canonical catalog, realtime WS/SSE contract |
| [[26_Task_Agent_Lifecycles]] | Task & agent state machines, supervisor rules |
| [[27_Security_Permissions]] | Permission gate, risk levels, dangerous ops, secret handling |
| [[28_Reliability_Operations]] | Idempotency, crash recovery, SQLite/Redis split, limits, degraded modes |
| [[29_Testing_Strategy]] | Test pyramid, chaos testing, E2E acceptance lifecycle |
| [[30_Implementation_Docs]] | Required `docs/implementation/` files & validation checklist |

## 🎯 Core Capabilities (at a glance)

- CLI + ChatGPT-style web dashboard (localhost)
- Sandboxed coding workspace with live diff/approval
- Supervisor-based task decomposition and subagent orchestration
- Persistent memory: vector DB + Obsidian vault
- Research & browser automation with session recording
- Document generation (PPTX/PDF/DOCX/XLSX)
- Autopilot (desktop control via accessibility tree + vision + input control)
- Scheduler (cron/interval/date/webhook)
- Permission & trust model with approval queues

## ✅ Non-Goals (v1–v3)

Multi-tenant, cloud, billing, mobile. Multi-device sync is a stretch goal.

## 🚦 Project Status

- **Phase:** Build implemented — Phases 0–21 (see `agent-system/docs/implementation/STATUS.md`)
- **Next action:** E2E acceptance + remaining backlog in STATUS.md (`Next` section)
- **Contract:** [[23_Engineering_Contract]] governs all implementation
