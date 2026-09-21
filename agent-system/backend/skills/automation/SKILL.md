---
name: automation
version: 0.1.0
description: Create, schedule, and maintain automated tasks, cron jobs, and event-driven workflows.
enabled: true
agents:
  - react
  - chat
config: {}
author: builtin
---

# Automation Skill

## Purpose
Set up background schedules, recurring tasks, reminders, and automated workflow triggers.

## When to Use
Use when users ask to schedule reminders ("Remind me tomorrow"), set up daily summaries ("Every morning summarize this"), or build automated triggers.

## Workflow
1. Parse schedule requirement (e.g. interval, cron expression, or delay).
2. Register scheduled job or task trigger in the scheduler store.
3. Confirm schedule configuration and payload.

## Required Tools
- `tasks` / `scheduler`

## Constraints
- Ensure job schedules survive process restarts.
- Support natural language schedule expressions.

## Output & Verification
- Confirmed schedule registration with target time and payload details.
