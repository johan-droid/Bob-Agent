---
title: Web Dashboard UI/UX
type: spec
project: Agent System
status: locked
updated: 2026-09-06
up: "[[00_Index]]"
---

# 5. Localhost Web Dashboard — UI/UX Specification

> **Source:** Spec v3.0 §5 · Next.js 15 + Tailwind + shadcn/ui · `localhost:3000`
>
> [!warning] v3.1 Frontend Rule (§33)
> The frontend must consume **real backend contracts** (`/api/v1`, [[18_API_Reference]]). NEVER create production UI that merely pretends functionality exists — no fake costs, agents, reasoning, approvals, task status, replay, analytics, downloads, or progress. Fixtures are allowed only in tests/development. Every production UI control must have a real backend implementation.
> All realtime views (reasoning trace, task progress, chat) must implement reconnect + resume-from-sequence per [[25_Event_System]].

## 5.1 Design System

### Color Palette

```css
--primary: #3b82f6       /* Bold blue, CTA buttons */
--primary-dark: #1e40af  /* Darker blue, hover */
--secondary: #8b5cf6     /* Purple, agent status */
--success: #10b981       /* Green, task complete */
--warning: #f59e0b       /* Orange, needs attention */
--danger: #ef4444        /* Red, errors */
--neutral-50: #f9fafb
--neutral-100: #f3f4f6
--neutral-900: #111827
--bg-dark: #0f172a       /* Deep navy for dark mode */
--text-primary: #1f2937
--text-secondary: #6b7280
--border: #e5e7eb
```

### Typography

```
Font stack: Inter, system-ui, sans-serif
Display (H1): 32px, weight 700, line-height 1.2
Heading (H2): 24px, weight 600, line-height 1.3
Title (H3): 20px, weight 600, line-height 1.4
Body: 14px, weight 400, line-height 1.6
Small: 12px, weight 400, line-height 1.5
Code: Fira Code, 12px, weight 400, monospace
```

### Spacing

Tailwind default 4px grid: 4, 8, 12, 16, 24, 32, 48, 64, 80, 96.

### Shadows & Depth

```
Shadow-sm: 0 1px 2px 0 rgba(0, 0, 0, 0.05)
Shadow-md: 0 4px 6px -1px rgba(0, 0, 0, 0.1)
Shadow-lg: 0 10px 15px -3px rgba(0, 0, 0, 0.1)
Shadow-xl: 0 20px 25px -5px rgba(0, 0, 0, 0.1)
Glass effect: backdrop-blur-md, bg-white/80, border border-white/20
```

## 5.2 Main Layout

```
┌─────────────────────────────────────────────────────────────┐
│  Logo | Breadcrumb              [Search] [Notifications] [⚙] │  Header (sticky)
├──────────┬────────────────────────────────────────────────────┤
│ Sidebar  │  MAIN CONTENT AREA (responsive)                  │
│ (collap) │                                                    │
│ ↓        │  ↓ Below: 11 Main Tabs                            │
└──────────┴────────────────────────────────────────────────────┘
```

### Sidebar (left, collapsible)

- Logo + system name ("Agent System") at top.
- Main navigation:
  - 🏠 Dashboard
  - 💬 Chat
  - 📊 Kanban
  - 🖥️ Workspace
  - 📚 Vault
  - 📁 Outputs
  - 📅 Schedule
  - ✅ Approvals (with red badge count if pending)
  - 📋 Templates
  - 💰 Cost
  - 📖 Recipes
  - 🔍 Reasoning
  - 📊 Insights
  - 🔧 Settings
  - 📜 Audit Log
- Footer: connection status, service uptime, quick stats (active agents, queue depth).

### Sidebar styling

- Dark background (`--bg-dark` or `--neutral-900`).
- Light text.
- Hover: subtle background shift.
- Active tab: left border accent (`--primary`), bold text.
- Collapse button: hamburger icon, smooth animation.

### Header (top)

- Left: Logo + breadcrumb trail (e.g., "Agent System > Chat > Session #5").
- Center: search input (full-width, autocomplete: agents, tasks, vault notes).
- Right:
  - Bell icon (notifications, unread count badge).
  - Settings icon (quick toggles: dark mode, notifications on/off).
  - User avatar (for multi-device future, greyed out now).

## 5.3 Chat Tab (localhost:3000/chat)

```
┌────────────────────────────────────┐
│  Chat History / Sessions Selector  │  (collapsible sidebar)
│  [New Chat] [Session #5] [Session] │
├────────────────────────────────────┤
│  Chat Messages Area (scrollable)    │
│                                    │
│  ┌───────────────┐                │
│  │ User: "Build  │  (message bubble)
│  │ a fast API"   │  Message styling:
│  └───────────────┘  - User: blue, right-aligned
│                     - Agent: grey, left-aligned
│  ┌─────────────────────────────────┐
│  │ Agent: "I'll set up...          │
│  │ [START] Framework scaffolding   │
│  │         ✓ Complete              │
│  │ [FIX]   Database migration      │
│  │         ⏳ Running               │
│  └─────────────────────────────────┘
│                                    │
├────────────────────────────────────┤
│ Input: [Type your message here...] │
│        [Attach file] [Emoji] [Send]│
└────────────────────────────────────┘
```

### Agent message enhancement

- Inline badges for task status: [✓ Complete], [⏳ Running], [✗ Failed].
- Expandable task details: click badge to see full task output/logs.
- Code snippets: inline syntax highlighting (Prism.js), copy button.
- Links to other tabs: "View in Workspace" button on code-related messages.

### Right-side panel (when selected)

- Active agents list: agent name, type, status (green/yellow/red), resource usage (CPU%, memory%).
- Kill button per agent.

### Animations

- New message slides in from bottom with fade.
- Typing indicator (three bouncing dots) when agent is generating.
- Token count update live in top-right (e.g., "1,250 tokens used").

## 5.4 Kanban Tab (localhost:3000/kanban)

```
┌──────────────────────────────────────────────────────────┐
│ [Goal Input] ▶ Submit Goal                               │
├──────────────────────────────────────────────────────────┤
│                                                           │
│  Pending      In Progress   Review        Done    Failed │
│  ┌──────┐     ┌──────┐     ┌──────┐      ┌────┐  ┌────┐ │
│  │ t1   │     │ t2   │     │ t3   │      │ t4 │  │ t5 │ │
│  │ Desc │ --> │(🟡)  │ --> │(👁️) │  --> │ ✓  │  │ ✗  │ │
│  └──────┘     └──────┘     └──────┘      └────┘  └────┘ │
│                                                           │
│  ┌──────┐                                                 │
│  │ t6   │                                                 │
│  │ ...  │                                                 │
│  └──────┘                                                 │
└──────────────────────────────────────────────────────────┘
```

### Card styling

- Each task = card with title, agent type (badge), status icon, progress bar (if applicable).
- Colors:
  - Pending: `--neutral-200`
  - In Progress: `--primary` (blue)
  - Review: `--warning` (orange)
  - Done: `--success` (green)
  - Failed: `--danger` (red)
- Click card to expand: show full task details, logs, dependencies, estimated time.
- Drag-and-drop within columns (for user override, low-priority).
- Batched tasks: grouped card with "Batch" badge, expandable to show members ([[11_Feature_Task_Batching]]).

### Dependency visualization

- Arrows between cards showing task dependencies.
- Hover arrow to highlight path.

### Top toolbar

- View selector: [Timeline] [Kanban] [Tree/DAG] buttons.
- Filter: agent type, status, tag, date.
- Sort: by priority, by agent, by time, by cost.

## 5.5 Workspace Tab (localhost:3000/workspace)

```
┌─────────────────────────────────────────────────────────┐
│ [New Workspace] [Open...] [Workspace selector dropdown] │
├──────────────────┬──────────────────────────────────────┤
│ File Tree        │ Editor / Diff Viewer / Terminal        │
│ (left, narrow)   │                                        │
│                  │ [Tabs: Editor | Diff | Terminal | QA] │
│ 📁 src/          │                                        │
│  ├ main.py       │ Content area (syntax-highlighted)      │
│  ├ utils.py      │                                        │
│ 📁 tests/        │                                        │
│ 📄 .gitignore    │                                        │
│ 📄 requirements  │                                        │
│                  │ [Bottom toolbar]                       │
│ [Template Snap]  │ [Run | Test | Commit | Diff | Log]    │
│ [Clone]          │                                        │
└──────────────────┴──────────────────────────────────────┘
```

### Left panel (file tree)

- Collapsible folders with icons.
- Right-click menu: new file, delete, rename, copy path.
- Syntax highlighting icon indicator (`.py` = Python icon, `.json` = JSON icon, etc.).
- Click file to open in editor.

### Center panel (editor/diff/terminal)

- **Editor tab:** syntax highlighting (Highlight.js), line numbers, code folding, dark/light themes.
- **Diff tab:** before/after view with line-by-line coloring (red=removed, green=added). Clickable "Approve this change" / "Reject" buttons.
- **Terminal tab:** live output stream from agent's shell commands. Black background, green text (optional retro theme). Copy-to-clipboard button per output block.
- **QA tab:** test results, coverage badges, HTML report embed ([[12_Feature_Autonomous_QA]]).

### Right panel (when active)

- Git log: recent commits with author (CodeAgent), timestamp, message.
- Workspace info: disk usage, last modified, sandbox container ID.
- "Save as Template" button ([[08_Feature_Workspace_Templates]]).
- "Clone from Template" dropdown.

### Bottom toolbar

- [Run Tests] button: triggers QA pipeline, shows live results.
- [Git Diff] button: shows all uncommitted changes.
- [Commit] button: prompts for message, commits (signed as CodeAgent).
- [Kill Sandbox] button: force-restart the workspace container.

### Animations

- File tree expands/collapses smoothly.
- Tab switching fades in.
- Diff highlighting animates (blink effect on changed lines).

## 5.6 Vault Tab (localhost:3000/vault)

```
┌────────────────────────────────────────────────────────┐
│ [Search input] [Create new note] [Obsidian sync status]│
├─────────────────────┬────────────────────────────────┤
│ Note List           │ Note Content                    │
│ (tree or flat)      │ (markdown preview/edit)         │
│                     │                                 │
│ 📌 Daily Notes      │ # Decision: Auth System        │
│ ├ 2026-09-06        │ **Date:** 2026-09-05            │
│ ├ 2026-09-05        │ **Tags:** [auth, security]      │
│ 📁 Decisions        │ **Related:**                     │
│ ├ Auth Reconciliat  │ [[User Model]] [[JWT Strategy]] │
│ ├ Memory Vault      │                                 │
│ 📁 Projects         │ We decided to use Better Auth   │
│ ├ GraftAI           │ because...                      │
│ ├ VibeCoder         │                                 │
│ 📁 Agents           │ ---                             │
│ └ Research          │ [Backlinks to this note]        │
│                     │ - [[Project Dashboard]]         │
│                     │ - [[LLM Decision]]              │
└─────────────────────┴────────────────────────────────┘
```

### Left panel (notes tree)

- Hierarchical folder structure mirroring Obsidian vault layout.
- Search input filters in real time (full-text on title + content).
- "Create new note" button opens a form: title, parent folder, tags, template.
- Sync status indicator: green checkmark if synced with local Obsidian, yellow if pending, red if conflict.

### Right panel (note content)

- Markdown renderer (Remark/Rehype) with syntax highlighting.
- Wiki-link rendering: `[[Note Name]]` → clickable link to that note.
- Backlinks section at bottom: which other notes link to this one.
- Edit toggle (pencil icon): switches to CodeMirror editor with markdown preview side-by-side.
- Save button (if editing).
- Delete button with confirmation.

### Tag cloud (bottom-left)

All tags used across vault, clickable to filter.

### Animations

- Note list updates live if agent writes to vault (re-sorts, highlights new note).
- Wiki-links highlight on hover.

## 5.7 Outputs Tab (localhost:3000/outputs)

```
┌──────────────────────────────────────────────────────┐
│ Generated Files / Downloads                          │
├──────────────────────────────────────────────────────┤
│ Filter: [All] [PPTX] [PDF] [DOCX] [XLSX]             │
│ Sort: [Newest] [Size] [Name]                         │
│                                                      │
│ ┌─────────────────────────────────────────────────┐  │
│ │ 📊 Q3 Financial Report                          │  │
│ │ .pptx | 2.4 MB | Generated 2h ago               │  │
│ │ Task: #34 (Document Agent)                      │  │
│ │ [Preview] [Download] [Delete] [Open in Obsidian]│  │
│ └─────────────────────────────────────────────────┘  │
│                                                      │
│ ┌─────────────────────────────────────────────────┐  │
│ │ 📄 Research Summary                             │  │
│ │ .pdf | 1.8 MB | Generated 4h ago                │  │
│ │ Task: #28 (Document Agent)                      │  │
│ │ [Preview] [Download] [Delete]                   │  │
│ └─────────────────────────────────────────────────┘  │
│                                                      │
│ ┌─────────────────────────────────────────────────┐  │
│ │ 📈 Budget Analysis                              │  │
│ │ .xlsx | 512 KB | Generated 1d ago               │  │
│ │ Task: #15 (Document Agent)                      │  │
│ │ [Preview] [Download] [Delete]                   │  │
│ └─────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────┘
```

### Card per output

- Icon (file type), filename, size, created time, task reference.
- Preview button: opens in modal (PPTX → thumbnail carousel, PDF → embedded viewer, XLSX → table, DOCX → formatted view).
- Download button: direct download link.
- Delete button with confirmation.

### Bulk actions

- Select multiple files, bulk download as ZIP.
- Bulk delete with confirmation.

## 5.8 Additional Tabs (Brief)

### Schedule Tab (localhost:3000/schedule)

- Cron job list: name, schedule (human-readable), next run, last run status.
- [New Job] button: form to create cron/interval/date/webhook jobs.
- Job detail: show full config, run history table, manual trigger button.

### Approvals Tab (localhost:3000/approvals)

- Pending actions queue: action description, scope, context (screenshot/diff preview if available).
- [Approve] [Deny] [Always Allow This Type] buttons per action.
- Color-coded by scope: red = high-risk (browser:transact, os:input), yellow = medium (file:write).

### Templates Tab (localhost:3000/templates)

- Gallery of workspace templates: thumbnail, name, description, tags, use count.
- [New Workspace from Template] selector.
- [Save Current as Template] button.

### Cost Tab (localhost:3000/cost)

- Cost summary: current period total, budget remaining, % used, trend arrow.
- Breakdown chart: by agent type, by LLM model, by task.
- Recent LLM calls table: model, tokens, cost, latency.
- Recommendations section: suggested model switches or task deferrals.
- Budget settings form: set period, limit, alert threshold.

### Recipes Tab (localhost:3000/recipes)

- Recipe gallery: name, description, DAG visualization thumbnail, tags, usage count.
- [Create New Recipe] button (from existing task graph or from scratch).
- Recipe detail: full DAG view, parameter form, execution history.
- [Run Recipe] button: fill params, submit.

### Reasoning Tab (localhost:3000/reasoning)

- Session selector: list recent agent reasoning sessions.
- Real-time reasoning tree visualization (D3.js).
- Timeline scrubber, step-through controls.
- Decision node inspection sidebar.

### Insights Tab (localhost:3000/insights)

- Insights gallery: daily briefing, weekly summary, anomalies, trends.
- Card per insight: key findings summary, full HTML view link.
- Archive/delete buttons.

### Audit Log Tab (localhost:3000/audit)

- Table: actor (agent/user), action, scope, approval source, outcome, timestamp.
- Filter: by actor, by scope, by date range, by outcome (approved/denied/auto).
- Export button: CSV/JSON download.

### Settings Tab (localhost:3000/settings)

- Appearance: dark/light mode toggle, accent color picker.
- Agent settings: per-agent personality (tone, verbosity), trust levels per scope.
- Notification settings: email/push/dashboard.
- Service settings: port, bind address, enable/disable features.

## Related

- CLI spec: [[17_CLI_Specification]]
- API endpoints: [[18_API_Reference]]
