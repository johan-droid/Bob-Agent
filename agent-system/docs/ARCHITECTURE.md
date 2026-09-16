# Architecture

## Service map (`backend/src/agent_system/`)

```
api/main.py            FastAPI app + lifespan (builds all services into app.state)
api/v1/router.py       health/ready/auth-token · sessions · tasks · approvals ·
                       workspaces · artifacts · events
api/v1/features.py     recordings/replay · batches · recipes · personalities ·
                       insights · model-routing (providers list/test) · schedule ·
                       autopilot · model-calls · qa-reports
api/v1/realtime.py     realtime router (/api/v1) — WS/SSE fanout + resume
api/v1/telegram.py     bot webhook (header-secret) + status
api/v1/skills.py       skills CRUD + import (emits skill.* events)
api/v1/settings.py     settings read/write + groups (live config, auth required)
api/v1/a2a.py          agent-to-agent delegate/callback/delegations (off unless A2A_ENABLED)
cli/                   agentctl: main · chat (REPL w/ slash commands incl.
                       live config) · setup (wizard) · settings · skills · soul ·
                       sysdetect
services/orchestrator.py   Supervisor (goal → task DAG validation) + dispatch;
                           fallback to react_agent for unregistered agent types
services/agent_loop.py     ReAct loop: Reason+Act over the tool registry
                           (fenced ```tool:name blocks, <tool_result> feedback)
services/tools.py          ToolRegistry: shell · file_read/write/list · web_fetch ·
                           memory_recall/remember · tasks_inspect ·
                           openconnector_execute/list · mcp_call · mcp_list
services/mcp.py            MCP client: stdio (npx/uvx) + streamable HTTP
                           (SSE/JSON, Mcp-Session-Id) · implicit openconnector server
services/openconnector.py  oomol-lab/open-connector Runtime API: execute/list
                           actions, guides, health, MCP URL/headers, catalog
services/memory_hooks.py   remember_outcome/fact + recall_recent (Obsidian vault)
services/model_router.py   ModelRouter.invoke: soul → skills → adapter;
                           records ModelCall + model.*/cost.* events
services/providers.py      12 real HTTP adapters (OpenAI-compat, Gemini,
                           Anthropic native) + pricing + router builder
services/skills.py         SkillManager: discover/compose/configure/author
services/soul.py           SOUL.md lookup + <identity> block
services/telegram.py       polling/webhook gateway, allowlisted chats, approvals
services/permissions.py    PermissionGate (risk/scope decisions)
services/auth.py           token mint/verify (bootstrap secret → Bearer)
services/memory.py         Obsidian vault writer + recall + MemoryStore
services/backup.py       BackupService: SQLite online copy + vault/recordings snapshot (nightly-backup cron in api/main.py)
services/tool_plugins.py ToolPluginManager: folder-drop tools (tool.json + handler), sandbox-forced execute tier
services/a2a.py          A2AService: signed outbound delegation (off unless A2A_ENABLED, per-target approvals)
services/workspaces.py · recipes.py · recording.py · autopilot.py · recovery.py ·
services/batching.py · insights.py · personality.py · sandbox.py · secrets.py · …
agents/react_agent.py      llm_react_handler: goal → ModelRouter + run_tool_loop;
                           goal-aware fallback; memory recall w/ outcome persist
agents/registry.py         agent_type → handler (+ register_default swap-in)
agents/browser_research.py · documents.py · qa.py   specialist handlers
infra/                     SQLAlchemy engine/session, event bus, models
domain/                    ULIDs, event envelope
worker.py                  RQ worker → agents.react_agent.install() → run_agent
```

## Key flows

### Chat goal → execution → answered
`POST /api/v1/sessions {goal}` → session ACTIVE + `session.created` →
supervisor decomposes server-side into a task DAG → tasks land `QUEUED` →
**either** the RQ worker (`python -m agent_system.worker`, out-of-process) or
the in-process orchestrator picks one up → for a goal-bearing task the
`llm_react_handler` runs the **ReAct loop**:
`ModelRouter.invoke(soul+skills) → parse ```tool:name fences → execute →
<tool_result> fed back → repeat until the model answers or the iteration
budget (TOOLS_MAX_ITERS) is spent` → outcome persisted via `remember_outcome`
(a vault note, `memory_auto_remember`) → task SUCCEEDED with
`result_json {output, stopped, tool_calls, iterations, usage}` → REPL /
dashboard tails `GET /api/v1/events?session_id=…&after_sequence=…`.

**Honest fallback policy:** with no real provider configured
(`DEFAULT_PROVIDER=echo`), unregistered agent types keep the deterministic
builtin (`registry._builtin`) — tasks never pretend an LLM ran.

### ReAct tool protocol (provider-agnostic)
The model emits fenced blocks; the loop executes them and returns results:

```text
You have these tools…
```tool:file_write
{"path": "notes.md", "content": "hello"}
```

<tool_result name="file_write">
{"ok": true}
</tool_result>
```

Every tool execution emits `tool.called` / `tool.result` events (audit trail
in the DB). Unknown tools, approval blocks (`NeedsApprovalError`), and
crashes become error results the model can react to — never silent. Shell
runs in the Docker sandbox unless `TOOLS_SHELL_MODE=local` (explicit host
opt-in) or `off`; execute tools require a live approval row when
`TOOLS_REQUIRE_APPROVAL` is set.

### Model call composition (`ModelRouter.invoke`, the only prompt seam)
`[soul <identity>] + prompt + [skills <skills>]` → provider adapter
(OpenAI-compatible `/chat/completions`, Gemini `:generateContent` with
`x-goog-api-key`, Anthropic `/messages` with `x-api-key` +
`anthropic-version`) → `ModelCall` row + `model.completed/failed` (with
`skills_used`, `soul_used`) + `cost.recorded`. No skill manager / no soul
file = prompt passes through untouched. `react_agent._build_router`
self-heals offline defaults (zero-cost echo pricing entry + EchoProvider) so
`DEFAULT_PROVIDER=echo` is actually runnable end-to-end.

### OpenConnector (SaaS actions via HTTP + MCP)
`OPENCONNECTOR_BASE_URL` set → the tool registry gains
`openconnector_execute` (`POST /v1/actions/:id {input, connectionName}`) and
`openconnector_list` (discovery) **and** `services/mcp.py` appends an
implicit `openconnector` streamable-HTTP MCP server (`POST /mcp`, Bearer
runtime token + `x-oo-connector-alias`). Credentials stay inside the
OpenConnector runtime; action guides (`GET /api/actions/:id/agent.md`) are
fetchable for richer prompts. Docker: `docker compose up openconnector`
→ `http://localhost:3000`.

### MCP client (two transports, one interface)
`McpServerConfig`: `command`+`args` ⇒ stdio (`McpStdioClient`), `url`
⇒ streamable HTTP (`McpHttpClient`: JSON-RPC over POST, parses JSON **and**
SSE `event: message`/`data:`, replays `Mcp-Session-Id`, tolerates 202
notifications). `all_servers()` = `MCP_SERVERS` JSON + the implicit
OpenConnector server (an explicit entry named `openconnector` wins). Fresh
session per call; failures are recorded, never fatal.

### Workspace + sandbox execution
`/workspaces/{id}/exec` → DockerSandbox (CPU/mem caps, network off by
default) for untrusted steps; workspaces enforce traversal-safe roots, size
caps, and fingerprinting. `tools_fs_roots` expands file-tool jail roots.

### Skills lifecycle
Ship in `backend/skills/`, add via folder drop /
`skills add <path|.md-url|git-url>` / `POST /api/v1/skills[/import]`
(agents use the same endpoint with `author: "agent:<type>"`). Enable flags
persist in `skills/.state.json`, user values in `config.local.yaml` —
`SKILL.md` itself is never rewritten by tooling. Invalid skills are
reported in `last_errors`, never fatal.

### Memory / vault
`memory_hooks.remember_outcome` auto-writes scrubbed task-outcome notes
(never raises — memory must not break execution); `recall_recent` keyword-
ranks vault notes (newest + overlap first) and `memory_recall_top_k` injects
the top matches into ReAct task prompts. The chat exposes this directly:
`/memory <fact>` and `/recall <query>`.

### Config lifecycle
`.env.example` documents → `make setup` (quick path) or `settings wizard`
(exhaustive) writes `.env.local` → non-secret answers remembered in
`~/.config/bob-agent/setup.json` → server reads at boot → **live from the
chat REPL**: `/settings set/unset`, `/model set <provider> [model]`,
`/tools` (shell mode, MCP servers, OpenConnector), `/schedule rm`.
`settings check` validates.

### Auth
`AGENT_BOOTSTRAP_SECRET` → `POST /api/v1/auth/token` → Bearer token for all
`authenticated` routes. CLI auto-mints from local secrets (`--token` /
`AGENTCTL_TOKEN` override). Telegram webhook uses its own
`X-Telegram-Webhook-Secret` header check.
