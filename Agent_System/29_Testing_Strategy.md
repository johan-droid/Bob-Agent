---
title: Testing Strategy
type: spec
project: Agent System
status: locked
updated: 2026-09-06
up: "[[00_Index]]"
---

# Testing Strategy & Chaos Engineering (v3.1 §39, §40, §44)

## Test Pyramid

| Layer | Scope |
| --- | --- |
| **Unit** | Core domain/services |
| **Integration** | API+DB · API+Redis · worker+DB · worker+Redis · agent+sandbox · model router · scheduler · memory |
| **Contract** | API schemas (`/api/v1`) |
| **Security** | Path traversal · secret leakage · sandbox escape · unauthorized tool access · permission bypass · unsafe archive extraction |
| **Recovery** | Kill workers mid-execution · restart services · disconnect WebSockets · simulate Redis failure · simulate LLM timeout |
| **Replay** | Verify inspect/simulate/re-execute boundaries ([[09_Feature_Behavior_Recording]]) |
| **Migration** | Fresh DB · existing DB upgrade |
| **Frontend** | Critical user journeys ([[16_Dashboard_UIUX]]) |
| **CLI** | JSON schema and exit codes ([[17_CLI_Specification]]) |

## Chaos Testing (before declaring production-ready)

Intentionally test and require predictable failure:

- Worker crash · API restart · Redis restart · browser crash · container crash
- Network timeout · LLM timeout · database lock
- WebSocket disconnect
- Task cancellation · approval expiration
- Duplicate event · duplicate task submission

## End-to-End Acceptance Test

User goal: *"Create a workspace, inspect the repository, identify failing tests, fix the failures, run QA, summarize the changes, and produce a report."*

Expected lifecycle (verify after clean installation/restart):

```
Session created → Task created → Supervisor plans → Workspace created
→ CodeAgent starts → Model selected → Model usage recorded
→ Tool calls recorded → Decision/execution events emitted
→ Code changes made → Failure detected if present → Recovery executed
→ QA runs → Results recorded → Diff generated
→ Approval requested where required → Approval granted
→ Changes committed → Artifact generated → Audit trail completed
→ Memory updated → Insight generated where applicable
→ Final response returned
```

## Phase Gate Relation

A phase cannot close ([[23_Engineering_Contract]]) with failing critical tests, broken migrations, or security violations. Test status is recorded per phase in [[21_Progress_Log]] and [[30_Implementation_Docs]].
