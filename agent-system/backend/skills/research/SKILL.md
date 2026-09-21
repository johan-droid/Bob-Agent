---
name: research
version: 0.1.0
description: Conduct structured research, gather evidence, synthesize findings, and cite sources accurately.
enabled: true
agents:
  - react
  - chat
config: {}
author: builtin
---

# Research Skill

## Purpose
Gather multi-source information, analyze evidence, synthesize concise summaries, and cite all sources accurately.

## When to Use
Use when answering factual inquiries, technical investigations, market research, or deep topic explorations requiring external or repository evidence.

## Workflow
1. Define clear research scope and key questions.
2. Search web/documents/repository using available tools.
3. Cross-reference facts across multiple reliable sources.
4. Synthesize findings into structured, concise sections.
5. Verify citations and check for contradictions or gaps.

## Required Tools
- `web_search` / `browser` / `documents_read` / `filesystem`

## Constraints
- Never hallucinate citations, quotes, URLs, or statistics.
- State explicitly if information is uncertain or conflicting.

## Output & Verification
- Structured summary with inline references/urls.
- Verify that every cited fact corresponds to actual tool observation.
