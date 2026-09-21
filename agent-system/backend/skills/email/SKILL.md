---
name: email
version: 0.1.0
description: Check email accounts, read messages, and draft or send email communications truthfully.
enabled: true
agents:
  - react
  - chat
config: {}
author: builtin
---

# Email Skill

## Purpose
Manage email communications (Gmail or SMTP/IMAP connectors) truthfully and securely.

## When to Use
Use when users ask about email messages, checking inbox, or sending email notifications.

## Workflow
1. Check if email/Gmail integration is connected via CapabilityRegistry / CredentialStore.
2. If NOT connected, inform user clearly and explain how to set up (/setup google).
3. If connected, list or search messages as requested.
4. For sending emails, require user confirmation/approval before dispatch.

## Required Tools
- `openconnector_execute`

## Constraints
- NEVER pretend email access is connected when it is not.
- NEVER send emails without explicit user approval.

## Output & Verification
- Accurate message summary or sent confirmation.
