---
title: Feature — Autonomous QA & Testing
type: feature
project: Agent System
status: spec
updated: 2026-09-06
feature_no: 8
up: "[[00_Index]]"
---

# Feature 8 — Autonomous QA & Testing

> **Source:** Spec v3.0 §4 · **Size:** Medium (Phase 13 in v3.1 plan — [[19_Execution_Plan]])
>
> [!warning] v3.1 Refinements (§25)
> **Generated tests are untrusted code.** Run inside the sandbox with: no host filesystem · no host credentials · restricted network · CPU limit · memory limit · timeout.
> Reports must be structured: tests generated · executed · passed · failed · skipped · duration · coverage (when available) · failure diagnostics.

## What it does

When a CodeAgent finishes writing code, it auto-generates unit tests, runs them, and reports coverage. Catches regressions early.

## Execution

```
core/agents/qa_agent.py
├── TestGenerator    (LLM: "write tests for this code")
├── TestRunner       (pytest/mocha/etc. runner, language-aware)
├── CoverageAnalyzer (coverage.py, nyc, etc.)
└── ReportBuilder    (HTML report, metrics table)
```

## Database

```sql
qa_reports(
  id, task_id, code_file,
  tests_generated, tests_passed, tests_failed,
  coverage_pct,
  coverage_report_html,
  timestamp
)
```

See [[04_Data_Model]].

## Flow

1. CodeAgent commits a change to `workspace/src/main.py`.
2. QAAgent introspects: "this is Python, pytest is available."
3. Generates test suite based on the code and commit message.
4. Runs `pytest` inside the workspace container.
5. Captures pass/fail, coverage stats.
6. Logs report to `qa_reports`, stores HTML in `outputs/`.
7. If coverage < 80%, flags as "low coverage" in the task result.

## Dashboard UI (localhost:3000/workspace)

- QA tab: show QA reports for recent code commits.
- Test results table: test name, status, execution time.
- Coverage badges: by file, by function.
- "View Full Report" link: opens HTML coverage report.

## API

```
GET /api/qa/reports?task_id=...
  -> [{tests_generated, passed, failed, coverage_pct}]
GET /api/qa/reports/{id}/html
  -> HTML coverage report
```

Full reference: [[18_API_Reference]].

## Acceptance (DoD)

- [ ] Tests auto-generated and run after CodeAgent commits.
- [ ] Coverage stored and flagged when < 80%.
- [ ] Sandbox restrictions verified: no host FS/credentials, restricted network, CPU/memory caps, timeout ([[27_Security_Permissions]]).
- [ ] Structured report persisted (generated/executed/passed/failed/skipped/duration/coverage/diagnostics).

## Non-goals

- Integration test generation (v2). Unit tests only, v1.
