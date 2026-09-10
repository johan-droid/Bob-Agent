---
title: Decision Log
type: log
project: Agent System
status: active
updated: 2026-09-06
up: "[[00_Index]]"
---

# Agent System — Decision Log (ADR)

> Record every locked decision that changes the spec. Format: context → decision → consequences.

## ADR-001 — Lock Tech Stack to Python/FastAPI + Next.js 15

- **Date:** 2026-09-06
- **Context:** Need async orchestrator, rich Python AI ecosystem, familiar frontend.
- **Decision:** Python 3.12 + FastAPI backend; Next.js 15 + Tailwind + shadcn/ui frontend. See [[03_Tech_Stack]].
- **Consequences:** Rust CLI deferred to v3 wrapping the same HTTP API; no native desktop UI.

## ADR-002 — SQLite First, Postgres Optional

- **Date:** 2026-09-06
- **Context:** Zero-config local-first system.
- **Decision:** SQLite default via Alembic migrations; Postgres swap-in optional.
- **Consequences:** Avoid Postgres-specific features in queries; keep JSON columns portable.

## ADR-003 — LanceDB for Vectors

- **Date:** 2026-09-06
- **Context:** No external vector server desired.
- **Decision:** Embedded LanceDB for semantic memory alongside the Obsidian vault.
- **Consequences:** Vault notes are the human-readable layer; LanceDB the machine layer.

## ADR-004 — Process-Isolated Subagents

- **Date:** 2026-09-06
- **Context:** Agent crashes must not take down the orchestrator.
- **Decision:** Each subagent runs as an isolated process with asyncio supervision.
- **Consequences:** IPC overhead accepted; restart semantics required per agent type.

## ADR-005 — Approval Gate Before Risky Actions

- **Date:** 2026-09-06
- **Context:** Autopilot and browser actions can have real-world effects.
- **Decision:** Permission gate intercepts scoped actions; high-risk scopes always queue for human approval.
- **Consequences:** Adds latency for gated actions; audit log records every decision.

## ADR-006 — Obsidian Vault as Human Memory Layer

- **Date:** 2026-09-06
- **Context:** Memory must be dual-use (agent + human readable).
- **Decision:** Markdown + YAML frontmatter notes in the user's Obsidian vault; `[[wiki-links]]` for relationships.
- **Consequences:** Agent writes must respect vault conventions; sync conflicts surfaced in Vault tab.

## ADR-007 — Adopt v3.1 as the Engineering Contract

- **Date:** 2026-09-06
- **Context:** v3.0 defines WHAT to build but under-specifies reliability, security, and verification. Autonomous implementation needs an enforceable contract.
- **Decision:** Adopt v3.1 ([[23_Engineering_Contract]]) as governing contract: source-of-truth hierarchy, phase gate, Definition of Done, no-shortcut policy, frontend/CLI real-backend rules, layered dependency rules.
- **Consequences:** Smaller fully-functional feature set preferred over breadth; every feature requires persistence + tests + restart verification before "done".

## ADR-008 — SQLite Authoritative, Redis Ephemeral

- **Date:** 2026-09-06
- **Context:** v3.0 implied Redis-backed state; crash recovery requires durable truth.
- **Decision:** SQLite (WAL, FKs, busy timeout) is the authoritative store; Redis is queue/cache/coordination only ([[28_Reliability_Operations]]).
- **Consequences:** All state reconstructible after Redis loss; slightly more write traffic to SQLite.

## ADR-009 — Canonical Event System as Single Audit Surface

- **Date:** 2026-09-06
- **Context:** Traces, recordings, insights, and audit were speced as separate pipelines risking divergence.
- **Decision:** One event bus with the v3.1 envelope and catalog ([[25_Event_System]]); traces/recordings/insights/audit all derive from it.
- **Consequences:** Single ordering/dedup/resume implementation; features render persisted events instead of private streams.

## ADR-010 — Reasoning Trace Renamed & Scoped to Auditable Data

- **Date:** 2026-09-06
- **Context:** v3.0 promised token-level "reasoning" that providers may not expose and chain-of-thought may be private.
- **Decision:** Rename to **Decision & Execution Trace**; capture auditable events only; token data optional/provider-dependent ([[05_Feature_Reasoning_Trace_Viewer]]).
- **Consequences:** No false claims in UI; trace works uniformly across providers.

## ADR-011 — Security Defaults

- **Date:** 2026-09-06
- **Context:** Autonomous agents with host access, browsers, and payments are high-risk.
- **Decision:** Centralized permission gate with 4 risk levels and 5 policies; dangerous operations default-deny; autopilot disabled by default and built last ([[27_Security_Permissions]]).
- **Consequences:** Some friction on risky actions; audit records every decision; autopilot ships in Phase 18 only after audit/recording/recovery mature.

---
