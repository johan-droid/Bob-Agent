---
title: Security & Permissions
type: spec
project: Agent System
status: locked
updated: 2026-09-06
up: "[[00_Index]]"
---

# Security & Permission System (v3.1 §12–§14)

> Centralized Permission Gate. **Never allow an agent to bypass the permission system.**

## Risk Levels & Policies

Risk: `LOW · MEDIUM · HIGH · CRITICAL`

Policies: `ALLOW_ONCE · ALLOW_SESSION · ALLOW_WORKSPACE · ALLOW_ALWAYS · DENY`

Each approval record must contain:

```json
{
  "approval_id": "approval_...",
  "task_id": "task_...",
  "agent_run_id": "run_...",
  "requested_action": "browser.submit_payment",
  "risk": "CRITICAL",
  "scope": "browser:transact",
  "requester": "BrowserAgent",
  "decision": "approved | denied | expired",
  "timestamp": "...",
  "expiration": "...",
  "reason": "..."
}
```

The v3.0 Approvals UI ([[16_Dashboard_UIUX]] §5.8) and CLI `agentctl approvals` operate on these records; approvals also expire (→ `approval.expired` event).

## Dangerous Operations — Default Deny

- Host filesystem access
- Credential extraction (SSH keys, browser password stores, .env)
- Arbitrary host shell
- System user modification
- Firewall changes
- Bootloader changes
- Security software modification
- Payment operations
- Credential transmission
- Arbitrary destructive host operations

**Autopilot is disabled by default** and has its own permission boundary (v3.1 §41 Phase 18 — implemented last).

## Security Model (Default Architecture)

- Localhost binding
- Explicit authentication/session secret
- Restricted CORS
- CSRF protection where applicable
- Secret redaction in all outputs
- Environment-variable protection
- Workspace isolation ([[08_Feature_Workspace_Templates]], [[19_Execution_Plan]] Phase 5)
- Docker isolation with resource limits
- Path traversal prevention
- Safe archive extraction
- File-size and output-size limits

## Secret Exposure — Never

Never expose `.env`, private keys, API keys, tokens, credentials, database URLs, browser profiles, or secret environment variables through:

- Reasoning/Decision & Execution Trace UI ([[05_Feature_Reasoning_Trace_Viewer]])
- Logs and audit records
- Recordings ([[09_Feature_Behavior_Recording]])
- Workspace templates ([[08_Feature_Workspace_Templates]])
- Model prompts
- Error messages

Redaction happens at the event/logging boundary, not at the UI layer only.

## Security Testing

Covered by the security test suite in [[29_Testing_Strategy]]: path traversal, secret leakage, sandbox escape, unauthorized tool access, permission bypass, unsafe archive extraction.
