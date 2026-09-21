---
name: coding
version: 0.1.0
description: Design, implement, refactor, and verify clean code in sandboxed environments.
enabled: true
agents:
  - react
config: {}
author: builtin
---

# Coding Skill

## Purpose
Write clean, maintainable, tested software implementations that meet specifications.

## When to Use
Use when implementing features, refactoring existing code, writing scripts, or creating software components.

## Workflow
1. Inspect workspace structure and understand requirements.
2. Design solution and write code modifications.
3. Run existing tests, linter, and typechecker to verify correctness.
4. Add new tests covering new functionality and edge cases.
5. Confirm all checks pass before concluding.

## Required Tools
- `file_read` / `file_write` / `run_tests` / `run_linter` / `run_typecheck` / `shell_execute`

## Constraints
- Never edit build artifacts directly; edit source files.
- Always run tests and linter after modifying code.

## Output & Verification
- Working code changes verified by test execution output.
