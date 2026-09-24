---
name: github
version: 0.1.0
description: Manage GitHub repositories, inspect issues, review pull requests, and trigger CI workflows.
enabled: true
agents:
  - react
config: {}
author: builtin
---

# GitHub Skill

## Purpose
Interact with GitHub API and repositories to manage code, pull requests, issues, and workflow runs.

## When to Use
Use when users ask to check GitHub issues, create PRs, review code changes, or inspect repository status.

## Workflow
1. Verify GitHub connection status via credentials/capability registry.
2. Query issues, pull requests, or workflow runs.
3. Perform requested action (e.g. create issue, comment, or PR).
4. Verify action completed via API output.

## Required Tools
- `openconnector_list` / `openconnector_execute` / `git_status` / `git_diff` / `git_log` / `git_show`

## Constraints
- Verify GitHub integration is connected before attempting actions.
- Never hardcode or echo personal access tokens.

## Output & Verification
- Confirmed GitHub action result (e.g. PR URL or issue number).
