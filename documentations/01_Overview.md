---
title: Overview
type: spec
project: Agent System
status: locked
updated: 2026-09-06
up: "[[00_Index]]"
---

# 1. System Overview

> **Source:** Spec v3.0 §1 · **Status:** Locked (product vision)

> [!warning] v3.1 Supersedes Where Applicable
> This note is the v3.0 product vision. Implementation is governed by the **v3.1 engineering contract** ([[23_Engineering_Contract]]). Where they conflict, v3.1 wins — see [[24_Canonical_Domain_Model]], [[25_Event_System]], [[26_Task_Agent_Lifecycles]], [[27_Security_Permissions]], [[28_Reliability_Operations]].

## Summary

A locally-hosted, persistent, multi-agent AI system delivering autonomous task execution with human approval gates.

## Core Capabilities

1. **CLI + ChatGPT-style web dashboard** (localhost).
2. **Sandboxed coding workspace** with live diff/approval.
3. **Supervisor-based task decomposition** and subagent orchestration.
4. **Persistent memory:** vector DB + Obsidian vault.
5. **Research & browser automation** with session recording.
6. **Document generation** (PPTX/PDF/DOCX/XLSX).
7. **Autopilot** (desktop control via accessibility tree + vision + input control).
8. **Scheduler** (cron/interval/date/webhook).
9. **Permission & trust model** with approval queues.

## Unique Competitive Features (v3)

| # | Feature | Note |
| --- | --- | --- |
| 1 | Agent Reasoning Trace Viewer — live token-by-token LLM output with decision tree visualization | [[05_Feature_Reasoning_Trace_Viewer]] |
| 2 | Multi-Model Orchestration — per-task LLM selection | [[06_Feature_Multi_Model_Orchestration]] |
| 3 | Autonomous Error Recovery — introspect, propose fix, learn patterns | [[07_Feature_Error_Recovery]] |
| 4 | Workspace Templates & Cloning — snapshot/replay workspace state | [[08_Feature_Workspace_Templates]] |
| 5 | Agent Behavior Recording & Replay — timestamped replayable action logs | [[09_Feature_Behavior_Recording]] |
| 6 | Real-Time Cost Optimizer — token tracking, alerts, model selection | [[10_Feature_Cost_Optimizer]] |
| 7 | Intelligent Task Batching — auto-combine similar tasks | [[11_Feature_Task_Batching]] |
| 8 | Autonomous QA & Testing — generated test suites, self-testing | [[12_Feature_Autonomous_QA]] |
| 9 | Workflow Recipe Library — compose & reuse multi-agent workflows | [[13_Feature_Recipe_Library]] |
| 10 | Agent Personality & Tone Adaptation — learned preferences | [[14_Feature_Personality]] |
| 11 | Live Reasoning Visualization — real-time reasoning tree rendering | [[05_Feature_Reasoning_Trace_Viewer]] |
| 12 | Autonomous Insight Generation — scheduled summaries & anomaly alerts | [[15_Feature_Insight_Generation]] |

## Non-Goals (v1–v3)

- Multi-tenant
- Cloud deployment
- Billing
- Mobile
- Multi-device sync (stretch goal only)
