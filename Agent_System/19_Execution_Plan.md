---
title: Execution Plan
type: plan
project: Agent System
status: locked
updated: 2026-09-06
up: "[[00_Index]]"
---

# Execution Plan — v3.1 (Phases 0–19)

> **Source:** v3.1 §41 · Supersedes the v3.0 16-phase plan. Incremental; each phase has acceptance criteria.
> Gate rule ([[23_Engineering_Contract]]): **do not advance** with failing critical tests, broken migrations, security violations, unresolved data corruption, fake production functionality, or undocumented divergence. After each phase: tests → build → integration tests → update status docs → descriptive Git commit.

## Phase 0 — Repository Forensics
Inventory the entire system before modifying code ([[30_Implementation_Docs]]: REPOSITORY_INVENTORY).
**Accept:** repository inventory · architecture map · dependency map · test baseline.

## Phase 1 — Domain + Persistence
Canonical entities & IDs ([[24_Canonical_Domain_Model]]) · SQLite config (WAL, FKs, busy timeout) · Alembic migrations · repositories.
**Accept:** migrations pass · CRUD tests pass · restart persistence verified.

## Phase 2 — Event System
Canonical events ([[25_Event_System]]): persistence, ordering, sequence numbers, dedup.
**Accept:** event persistence · ordering · sequence numbers · duplicate handling · tests.

## Phase 3 — API + Permissions
API v1 ([[18_API_Reference]]) · authentication · permission gate · approvals ([[27_Security_Permissions]]).
**Accept:** unauthorized actions rejected · approvals persisted · API contract tests pass.

## Phase 4 — Orchestrator + Queue
Supervisor · task DAG · worker · agent lifecycle ([[26_Task_Agent_Lifecycles]]) · cancellation · recovery.
**Accept:** multi-step task succeeds · worker crash recovery succeeds.

## Phase 5 — Sandbox + Workspace
Secure workspace execution (Docker isolation, resource limits).
**Accept:** isolation tests · resource limits · workspace persistence · secret exclusion.

## Phase 6 — Browser + Research
Browser/research agents (Playwright), isolated sessions.
**Accept:** isolated browser sessions · permission checks · recording · failure recovery.

## Phase 7 — Documents
PPTX · DOCX · PDF · XLSX ([[20_Deployment]] deps).
**Accept:** generated files valid · artifact persistence · sandbox execution.

## Phase 8 — Memory + Vault
Obsidian integration · LanceDB · memory lifecycle & retrieval · memory layers ([[28_Reliability_Operations]] §Memory).
**Accept:** persistence · retrieval · secret filtering.

## Phase 9 — Dashboard
Chat · Kanban · Workspace · Vault · Outputs · Schedule · Approvals · Templates · Cost · Recipes · Reasoning · Insights · Audit · Settings ([[16_Dashboard_UIUX]]).
**Accept:** all production UI uses real APIs — no mocked functionality (v3.1 §33).

## Phase 10 — CLI
`agentctl` ([[17_CLI_Specification]]) using the same `/api/v1` contracts.
**Accept:** every implemented major operation accessible · JSON mode · stable exit codes · interactive mode.

## Phase 11 — Model Router + Cost
Provider registry · model selection · pricing registry · cost tracking · budgets ([[06_Feature_Multi_Model_Orchestration]], [[10_Feature_Cost_Optimizer]]).
v3.1 rules: provider adapters (no hardcoded fictional model IDs), provider-aware token metrics, unknown cost never crashes, budget levels 50/75/90/100%, per-task/session/daily/provider budgets ([[28_Reliability_Operations]]).
**Accept:** every invocation produces a ModelCall record · cost marked estimated/unknown explicitly · budget alerts fire.

## Phase 12 — Error Recovery
Classification · retry · recovery planner/executor · pattern learning ([[07_Feature_Error_Recovery]]).
v3.1 rules: never auto-retry destructive actions, permission failures, deterministic validation errors, or repeated identical failures; max retries + exponential backoff ([[28_Reliability_Operations]]).
**Accept:** classified failures recovered safely · patterns persisted · retry limits enforced.

## Phase 13 — QA
Autonomous QA ([[12_Feature_Autonomous_QA]]). Generated tests are **untrusted code** — run in sandbox with no host FS, no host credentials, restricted network, CPU/memory limits, timeout.
**Accept:** structured reports (generated/executed/passed/failed/skipped/duration/coverage/diagnostics) · sandboxed execution verified.

## Phase 14 — Templates
Workspace template system ([[08_Feature_Workspace_Templates]]).
v3.1 rules: templates never contain `.env`, credentials, API keys, private keys, browser sessions, or secret config; record template version + source metadata.
**Accept:** secret exclusion verified · versioned templates · clone fidelity.

## Phase 15 — Recording + Replay
Inspect · simulate · approved re-execute ([[09_Feature_Behavior_Recording]]).
v3.1 rules: never blindly re-execute historical actions; before re-execution compare workspace fingerprint, OS, dependencies, agent version, model config, recipe version, permissions; destructive/network/payment re-execution requires explicit approval.
**Accept:** all three modes boundary-tested · replay safety verified.

## Phase 16 — Batching + Recipes
Task batching ([[11_Feature_Task_Batching]]) · recipe execution ([[13_Feature_Recipe_Library]]).
v3.1 rules: batch only compatible tasks (agent type, workspace, permissions, dependencies, resources); recipes versioned, run through the canonical Task/Event system, never bypass permissions.
**Accept:** batch_id + idempotency · cancellation · partial failure · per-task results · recipe versioning.

## Phase 17 — Personality + Insights
Versioned personality · feedback ([[14_Feature_Personality]]) · insight generation ([[15_Feature_Insight_Generation]]).
v3.1 rules: personality never modifies security/permission policies or safety constraints; insights derived only from canonical persisted events, respect secret redaction, no fabricated analytics.
**Accept:** personality versioned & auditable · feedback loop works · insights sourced from events.

## Phase 18 — Autopilot (LAST)
Desktop automation only after permission system, audit, recording, resource limits, and recovery all work ([[27_Security_Permissions]]).
**Accept:** disabled by default · own permission boundary · fully audited.

## Phase 19 — Production Hardening
Full integration tests · security audit · chaos tests ([[29_Testing_Strategy]]) · migration tests · performance tests · UI smoke tests · CLI tests · restart tests · documentation audit ([[30_Implementation_Docs]]).
**Accept:** E2E acceptance lifecycle passes after clean install/restart · FINAL_REPORT.md written with honest completion labels.
