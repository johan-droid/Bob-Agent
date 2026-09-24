---
name: debugging
version: 0.1.0
description: Diagnose bugs, reproduce test failures, isolate root causes, and apply verified fixes.
enabled: true
agents:
  - react
config: {}
author: builtin
---

# Debugging Skill

## Purpose
Systematically isolate root causes of software errors, test failures, or crashes, and apply verified minimal fixes.

## When to Use
Use when investigating bug reports, failing tests, unexpected exceptions, or performance regressions.

## Workflow
1. Reproduce the failure with a minimal test or command execution.
2. Trace error stack trace and log output to the exact failing lines.
3. Formulate hypothesis and inspect related state and variable values.
4. Apply targeted code fix.
5. Re-run tests to verify fix resolves the issue without regression.

## Required Tools
- `read_source` / `repo_search` / `edit_source` / `run_tests` / `git_diff` / `shell`

## Constraints
- Do not make random environment or code changes without diagnostic evidence.
- Always verify that the targeted fix causes tests to pass.

## Output & Verification
- Root cause explanation + passing test suite execution output.
