---
name: project-planning
version: 0.1.0
description: Break down complex goals into milestones, tasks, dependencies, and actionable execution plans.
enabled: true
agents:
  - react
  - chat
config: {}
author: builtin
---

# Project Planning Skill

## Purpose
Decompose high-level project goals into clear milestones, task DAGs, resource requirements, and risk mitigations.

## When to Use
Use when users ask to plan a project, architect a system, organize multi-step work, or establish execution roadmaps.

## Workflow
1. Clarify project goals, constraints, and success criteria.
2. Decompose into sequential phases and discrete task units.
3. Identify task dependencies, risks, and verification gates.
4. Format roadmap clearly with actionable next steps.

## Required Tools
- `memory_remember` / `memory_recall` / `tasks_inspect`

## Constraints
- Keep plans realistic, concrete, and modular.
- Differentiate conversational planning from durable task execution.

## Output & Verification
- Structured Markdown project plan with milestones and task list.
