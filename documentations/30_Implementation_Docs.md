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

> **As-built status (verified 2026-09-16):** only `REPOSITORY_INVENTORY.md`,
> `STATUS.md`, `AGENT_STATE.md`, and `FINAL_REPORT.md` exist under
> `agent-system/docs/implementation/`. `ARCHITECTURE.md`, `OPERATIONS.md`,
> and `DECISIONS.md` exist one level up in `agent-system/docs/`. The rows
> marked **Pending** below have no standalone file — their content lives in
> the `Exists at` location (or is unwritten). Owners are suggestions for who
> should write them next.

| Repo path | Content | Owner phase | Status in repo |
| --- | --- | --- | --- |
| `REPOSITORY_INVENTORY.md` | Full repo audit: modules, entry points, dependency graph, DB schema, routes, tests, tech debt, dangerous/broken functionality | Phase 0 ([[19_Execution_Plan]]) | Exists (`docs/implementation/`) |
| `ARCHITECTURE.md` | As-built architecture & dependency layers ([[24_Canonical_Domain_Model]]) | continuous | Exists (`docs/ARCHITECTURE.md`) |
| `API.md` | As-built `/api/v1` contract ([[18_API_Reference]]) | Phase 3+ | **Pending** (interim: `docs/DEVELOPER_GUIDE.md` §9 + OpenAPI at `/docs`) — owner: API phase lead |
| `EVENTS.md` | As-built event catalog & envelope ([[25_Event_System]]) | Phase 2+ | **Pending** (interim: `docs/DEVELOPER_GUIDE.md` Appendix B + `docs/ARCHITECTURE.md` event-bus notes) — owner: event-system lead |
| `RECOVERY.md` | Crash-recovery runbook ([[28_Reliability_Operations]]) | Phase 4+ | **Pending** (interim: `docs/DEVELOPER_GUIDE.md` §8.2/§8.14 + `docs/OPERATIONS.md` troubleshooting) — owner: orchestrator/worker lead |
| `SECURITY.md` | As-built security model ([[27_Security_Permissions]]) | Phase 3+ | **Pending** (interim: `docs/DEVELOPER_GUIDE.md` §8.8/§8.22–§8.24) — owner: security lead |
| `THREAT_MODEL.md` | Threats, boundaries, mitigations | Phase 3+ | **Pending** — owner: security lead |
| `DECISIONS.md` | Engineering decision log (mirrors [[22_Decision_Log]]) | continuous | Exists (`docs/DECISIONS.md`) |
| `STATUS.md` | Phase/status snapshot | continuous | Exists (`docs/implementation/`) |
| `OPERATIONS.md` | Startup, backup, upgrade runbook ([[20_Deployment]]) | Phase 1+ | Exists (`docs/OPERATIONS.md`) |
| `TROUBLESHOOTING.md` | Known failure modes & fixes | continuous | **Pending** (interim: `docs/OPERATIONS.md` troubleshooting tables) — owner: on-call/ops |
| `AGENT_STATE.md` | Current phase · completed phases · current task · active blockers · tests last executed · known failures · remaining work · known limitations — enables another autonomous agent to resume safely | continuous | Exists (`docs/implementation/`) |
| `FINAL_REPORT.md` | Architecture summary · implemented features · changed files · schema · API/event/security/permission models · startup & test commands · results · deployment · known limitations · required credentials · recovery procedures · future work | end | Exists (`docs/implementation/`) |

## Completion Labels (FINAL_REPORT)

Clearly distinguish: **IMPLEMENTED · PARTIALLY IMPLEMENTED · CONFIGURATION REQUIRED · KNOWN LIMITATION · BLOCKED**. Never claim 100% completion if anything remains incomplete.

## Final System Validation Checklist

Fresh install ✓ · migrations ✓ · API starts ✓ · worker starts ✓ · Redis ✓ · frontend builds ✓ · CLI works ✓ · auth ✓ · permissions ✓ · tasks persist ✓ · tasks survive restart ✓ · agent lifecycle ✓ · event system ✓ · WS reconnect ✓ · SSE reconnect ✓ · sandbox ✓ · workspace isolation ✓ · browser agent ✓ · document generation ✓ · memory ✓ · scheduler ✓ · model router ✓ · cost tracking ✓ · error recovery ✓ · QA ✓ · templates ✓ · replay safety ✓ · batching ✓ · recipes ✓ · personality ✓ · insights ✓ · audit ✓ · autopilot gated ✓ · no secret leaks ✓ · security tests ✓ · chaos tests ✓ · migration tests ✓ · UI smoke ✓ · CLI tests ✓ · docs match implementation ✓
