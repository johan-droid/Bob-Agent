---
title: Implementation Docs Index
type: moc
project: Agent System
status: active
updated: 2026-09-06
up: "[[00_Index]]"
---

# Implementation Documentation Contract (v3.1 §36–§37, §46)

> When implementation begins, these living documents must exist in the repository at `docs/implementation/` and evolve with the code. This note is the master index and status tracker; vault notes hold the specs, repo docs hold the as-built truth.

## Required Documents

| Repo path | Content | Owner phase |
| --- | --- | --- |
| `REPOSITORY_INVENTORY.md` | Full repo audit: modules, entry points, dependency graph, DB schema, routes, tests, tech debt, dangerous/broken functionality | Phase 0 ([[19_Execution_Plan]]) |
| `ARCHITECTURE.md` | As-built architecture & dependency layers ([[24_Canonical_Domain_Model]]) | continuous |
| `API.md` | As-built `/api/v1` contract ([[18_API_Reference]]) | Phase 3+ |
| `EVENTS.md` | As-built event catalog & envelope ([[25_Event_System]]) | Phase 2+ |
| `RECOVERY.md` | Crash-recovery runbook ([[28_Reliability_Operations]]) | Phase 4+ |
| `SECURITY.md` | As-built security model ([[27_Security_Permissions]]) | Phase 3+ |
| `THREAT_MODEL.md` | Threats, boundaries, mitigations | Phase 3+ |
| `DECISIONS.md` | Engineering decision log (mirrors [[22_Decision_Log]]) | continuous |
| `STATUS.md` | Phase/status snapshot | continuous |
| `OPERATIONS.md` | Startup, backup, upgrade runbook ([[20_Deployment]]) | Phase 1+ |
| `TROUBLESHOOTING.md` | Known failure modes & fixes | continuous |
| `AGENT_STATE.md` | Current phase · completed phases · current task · active blockers · tests last executed · known failures · remaining work · known limitations — enables another autonomous agent to resume safely | continuous |
| `FINAL_REPORT.md` | Architecture summary · implemented features · changed files · schema · API/event/security/permission models · startup & test commands · results · deployment · known limitations · required credentials · recovery procedures · future work | end |

## Completion Labels (FINAL_REPORT)

Clearly distinguish: **IMPLEMENTED · PARTIALLY IMPLEMENTED · CONFIGURATION REQUIRED · KNOWN LIMITATION · BLOCKED**. Never claim 100% completion if anything remains incomplete.

## Final System Validation Checklist

Fresh install ✓ · migrations ✓ · API starts ✓ · worker starts ✓ · Redis ✓ · frontend builds ✓ · CLI works ✓ · auth ✓ · permissions ✓ · tasks persist ✓ · tasks survive restart ✓ · agent lifecycle ✓ · event system ✓ · WS reconnect ✓ · SSE reconnect ✓ · sandbox ✓ · workspace isolation ✓ · browser agent ✓ · document generation ✓ · memory ✓ · scheduler ✓ · model router ✓ · cost tracking ✓ · error recovery ✓ · QA ✓ · templates ✓ · replay safety ✓ · batching ✓ · recipes ✓ · personality ✓ · insights ✓ · audit ✓ · autopilot gated ✓ · no secret leaks ✓ · security tests ✓ · chaos tests ✓ · migration tests ✓ · UI smoke ✓ · CLI tests ✓ · docs match implementation ✓
