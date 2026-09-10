---
title: v3.1 Engineering Contract
type: contract
project: Agent System
status: active
updated: 2026-09-06
up: "[[00_Index]]"
---

# v3.1 — Autonomous Implementation & Reliability Specification (Engineering Contract)

> v3.0 ([[01_Overview]]) is the **product vision**. This note is the **engineering contract**.
> On conflict, the source-of-truth hierarchy below wins.

## Source-of-Truth Hierarchy

1. Existing repository reality
2. This v3.1 engineering contract
3. Existing v3.0 product requirements
4. Existing tests
5. Existing documentation
6. Existing implementation assumptions

Before changing architecture, inspect the repository. Never assume v3.0's proposed files already exist; never duplicate an existing subsystem when it can safely be extended. If architecture conflicts with this contract: document the conflict, choose the safest coherent migration path, preserve functionality, update docs, add migration steps, test the migration.

## Primary Objective

Build a system that is: locally deployable · persistent · observable · recoverable · secure by default · modular · testable · restart-safe · permission-aware · cost-aware · multi-agent · extensible · UI-accessible · CLI-accessible · genuinely autonomous within defined boundaries.

Do not optimize for feature count. Optimize for: correctness, reliability, security, recoverability, maintainability, observability, integration, UX. **A smaller fully functional system beats a huge collection of fake features.**

## Autonomous Execution Policy

Make reasonable engineering decisions autonomously; do not stall on routine choices (naming, tests, refactors, documentation). Record non-trivial assumptions in the Decision Log ([[22_Decision_Log]]) and [[30_Implementation_Docs]] statuses.

Stop only when genuinely blocked by something that cannot safely be inferred: missing production credentials, unavailable external service, or destructive irreversible external action requiring human authorization.

When blocked:
1. Implement everything possible without the dependency.
2. Create a clean adapter/interface.
3. Add configuration placeholders.
4. Add tests using deterministic local test doubles.
5. Document the exact blocker.
6. Continue with all unrelated work.

**Never use missing credentials as an excuse to leave the architecture incomplete.**

## Phase Gate

Do **not** move to the next phase if the current phase has: failing critical tests, broken migrations, security violations, unresolved data corruption, fake production functionality, or undocumented architectural divergence.

After each successful phase: run tests → run build → run integration tests → update status notes ([[30_Implementation_Docs]], [[21_Progress_Log]]) → create a descriptive Git commit. Never force-reset user work, delete unknown user files, or rewrite unrelated history.

## Definition of Done

A feature is NOT complete because: code compiles, endpoint exists, UI renders, tests were skipped, mock data works.

A feature is complete only when:
1. Implementation exists
2. Integration exists
3. Persistence works
4. Error handling exists
5. Security boundaries exist
6. API contract exists
7. UI/CLI integration exists where applicable
8. Tests exist
9. Restart behavior is verified
10. Documentation is updated
11. No fake production behavior exists
12. Phase acceptance criteria pass

## No-Shortcut Policy

Never solve implementation problems by: commenting out failing code, weakening tests, deleting features, replacing real functionality with mocks, hardcoding success, swallowing exceptions, hiding errors, disabling security checks, bypassing permissions, adding TODOs instead of required behavior, returning fake API responses, embedding fake analytics, or creating placeholder backend routes.

If something cannot be implemented safely: **document it explicitly.**

## Frontend & CLI Rules

- **Frontend ([[16_Dashboard_UIUX]]):** must consume real backend contracts. No fake costs, agents, reasoning, approvals, task status, replay, analytics, downloads, or progress. Fixtures allowed only in tests/development.
- **CLI ([[17_CLI_Specification]]):** must use the same API/domain contracts as the web dashboard. Support `--json`, `--verbose`, `--no-color`. JSON output machine-readable and stable. No separate CLI-only business logic.

## Architectural Dependency Rules

```
API
 ↓
Application Services
 ↓
Domain
 ↓
Infrastructure
```

Do NOT allow: Frontend → database · Agent → raw SQL · Agent → host Docker socket · Feature → another feature's private tables · UI → direct filesystem mutation · CLI → duplicated business logic. Use repositories/services/interfaces.

## Documentation Contract

Maintain living implementation docs (see [[30_Implementation_Docs]]): ARCHITECTURE, API, EVENTS, RECOVERY, SECURITY, THREAT_MODEL, DECISIONS, STATUS, OPERATIONS, TROUBLESHOOTING, REPOSITORY_INVENTORY, AGENT_STATE, FINAL_REPORT. Documentation must evolve with implementation — never knowingly describe behavior the implementation does not have.

## Final Directives

CORRECTNESS > COMPLETENESS > CLEVERNESS · RELIABILITY > FEATURE COUNT · SECURITY > CONVENIENCE · REAL IMPLEMENTATION > MOCKS · OBSERVABILITY > MAGIC · RECOVERY > ASSUMPTIONS · DOCUMENTED BEHAVIOR > IMPLICIT BEHAVIOR.

The final system must be something another engineer can clone, start, inspect, debug, recover, extend, and trust.
