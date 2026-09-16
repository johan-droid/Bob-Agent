---
name: document-craft
version: 1.0.0
description: Structure and quality rules for generated documents (pptx/docx/xlsx/pdf).
enabled: true
agents:
- documents
config:
  default_tone: professional
  max_pages: 20
author: builtin
---

# Document Craft

You are helping a document agent produce files people actually want to read.

1. Start every document with a one-paragraph executive summary, then a table
   of contents for anything longer than 3 pages.
2. One idea per slide/section. Prefer tables and bullets over walls of text.
3. Use the `default_tone` config for wording unless the task says otherwise.
4. Keep the whole document under `max_pages` pages — cut ruthlessly rather
   than padding.
5. Filenames are lowercase with dashes (e.g. `q3-launch-plan.pptx`), never
   spaces. Report the exact output path when done.
