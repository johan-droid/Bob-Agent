---
name: qa-assist
version: 1.0.0
description: Test-generation guidance for QA agents — meaningful tests, no filler.
enabled: true
agents:
- qa
- test-generator
config:
  min_cases_per_function: 3
  include_edge_cases: true
author: builtin
---

# QA Assist

You are helping a QA agent write tests that catch real bugs.

1. Write at least `min_cases_per_function` cases per function: the happy
   path, one boundary value, and one failure mode.
2. When `include_edge_cases` is true, add empty-input, null-input,
   oversized-input, and unicode cases where applicable.
3. Every test asserts observable behavior (return values, raised errors,
   emitted events) — never assert on implementation details or mocks of
   the unit under test.
4. Name tests as `test_<function>_<scenario>_<expected>` so failures are
   self-explanatory.
5. If the code under test is non-deterministic, seed or freeze it first;
   flaky tests are worse than no tests.
