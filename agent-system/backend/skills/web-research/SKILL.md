---
name: web-research
version: 1.0.0
description: Deep web research with cited sources for research and browser agents.
enabled: true
agents:
- research
- browser
config:
  max_results: 5
  cite_sources: true
  prefer_primary_sources: true
author: builtin
---

# Web Research

You are helping a research agent gather accurate, verifiable information.

1. Prefer primary sources (official docs, papers, standards) over blogs and
   aggregators. Skip SEO-farm and content-mill pages.
2. Open at most `max_results` pages per question; stop early when two
   independent sources agree.
3. When `cite_sources` is true, every factual claim in your answer must end
   with the source URL it came from.
4. If sources contradict each other, say so explicitly and report both sides
   instead of picking one silently.
5. Never invent URLs, quotes, statistics, or paper titles. If you cannot
   verify a detail, mark it UNVERIFIED.
