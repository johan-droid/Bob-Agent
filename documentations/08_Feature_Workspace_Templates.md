---
title: Feature — Workspace Templates & Cloning
type: feature
project: Agent System
status: spec
updated: 2026-09-06
feature_no: 4
up: "[[00_Index]]"
---

# Feature 4 — Workspace Templates & Cloning

> **Source:** Spec v3.0 §4 · **Size:** Small (Phase 14 in v3.1 plan — [[19_Execution_Plan]])
>
> [!warning] v3.1 Refinements (§14, §22)
> Templates must **never** contain: `.env` · credentials · API keys · private keys · browser sessions · secret configuration.
> Allowed: source · dependency lockfiles · configuration templates · Git metadata (only where explicitly enabled) · documentation.
> Record **template version and source metadata**. Template creation goes through the permission gate ([[27_Security_Permissions]]) with secret scanning before snapshot.

## What it does

Snapshot a successful coding workspace state (files, env, git history) into a template. Reuse by cloning for similar future tasks. Enables rapid iteration and reduces setup time.

## Execution

```
core/sandbox/workspace_templates.py
├── TemplateSnapshotter (tar + metadata)
├── TemplateRegistry    (store, version, catalog)
└── TemplateCloner      (restore from template)
```

## Database

```sql
workspace_templates(
  id, name, description,
  source_workspace_id,
  snapshot_path,        -- tar.gz in templates/
  git_history_json,     -- commits from source
  tags_json,            -- ["python", "fastapi", "postgres"]
  created_at, used_count
)

-- workspace_sessions (extended)
template_id,            -- nullable, which template this came from
cloned_from_template    -- boolean
```

See [[04_Data_Model]].

## Flow

1. User completes a successful project (e.g., FastAPI + SQLite scaffold).
2. Clicks "Save as Template" in Workspace tab.
3. System snapshots: all files, `.env`, git history, Python dependencies (`requirements.txt`).
4. Stores in `templates/<template_id>/` with metadata in `workspace_templates`.
5. Next time: user starts new workspace, selects template from dropdown.
6. System clones template, restores files, re-initializes git, venv.

## Dashboard UI (localhost:3000/workspace)

- Templates sidebar: list of saved templates with tags and use count.
- "Save as Template" button on active workspace.
- "Clone from Template" button to create new workspace.
- Template detail view: shows source project, tags, size, git history, quick stats.

## API

```
POST   /api/workspace/{id}/save-template
  -> {name, description, tags}
GET    /api/templates
  -> list templates
POST   /api/workspace/clone-from-template/{template_id}
  -> creates new workspace from template
DELETE /api/templates/{id}
  -> remove template
```

Full reference: [[18_API_Reference]].

## Acceptance (DoD)

- [ ] Save workspace as template.
- [ ] Create new workspace from template — files restored exactly.
- [ ] Secret exclusion verified: `.env`, keys, credentials never present in snapshots.
- [ ] Template version + source metadata recorded.

## Non-goals

- Template versioning/branching (v2).
