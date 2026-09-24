---
name: deployment
version: 0.1.0
description: Prepare, package, deploy, and verify application deployments on cloud platforms.
enabled: true
agents:
  - react
config: {}
author: builtin
---

# Deployment Skill

## Purpose
Automate deployment procedures (Heroku, Docker, SSH, remote servers) with health checks and post-deploy verification.

## When to Use
Use when packaging apps, deploying code updates, checking server status, or managing release pipelines.

## Workflow
1. Verify target environment credentials and release assets.
2. Run pre-deployment test suite and build steps.
3. Execute deployment command / API push.
4. Perform post-deploy health check (`/health` or ping).
5. Confirm operational status before reporting completion.

## Required Tools
- `shell` / `ssh_execute` / `openconnector_execute`

## Constraints
- Never report deployment success without verifying live health endpoint or return status.
- High-risk deployment actions require approval.

## Output & Verification
- Deployment log + verified post-deploy health response.
