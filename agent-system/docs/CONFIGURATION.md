# Configuration

Precedence: **environment > `.env.local` > `.env` > built-in defaults.**
`.env.example` (repo root) documents everything; copy to `.env` for shared
defaults, keep secrets in `.env.local` (gitignored).

Manage it without editing files:

```bash
agentctl settings list [--group core|providers|routing|auth|telegram|storage|tools|memory|integrations|scheduler|cost|limits]
agentctl settings get GROQ_API_KEY
agentctl settings set DAILY_BUDGET_USD 7.5
agentctl settings set DEFAULT_PROVIDER groq
agentctl settings unset MAX_RETRIES
agentctl settings check
agentctl settings wizard [--yes]   # exhaustive first-run pass over everything
make setup                         # quick path (providers/auth/telegram/storage/limits)
```

`set` validates types plus field rules (known provider, http(s) URLs, JSON
object headers) and re-validates through the `Settings` model before
writing. Secrets are masked everywhere unless `--show-secrets`. The catalog
is generated from the `Settings` model, so this table cannot drift:

| Key | Type | Secret | Default | Help |
|-----|------|--------|---------|------|
| AGENT_ENV | str | no | dev | Environment name (dev/prod). |
| API_PORT | int | no | 8000 | Backend HTTP port (local dev). |
| PORT | int | no | (unset) | Platform-assigned port (Heroku $PORT); `effective_port` = PORT or API_PORT. |
| HEROKU_JAIL | bool | no | false | Cloud execution: route shell + execute-plugins to SubprocessJail (no Docker). |
| HEROKU_SHELL_ALLOWLIST | str | no | (unset) | Optional comma-separated binary prefixes jail shell must start with. |
| CLOUD_INLINE_RUN | bool | no | false | Cloud execution: drive Telegram goals in-process (no RQ worker). |
| CLOUD_VAULT_DB | bool | no | false | Cloud vault: memory notes to DB (`memory_notes`) instead of Obsidian files. |
| REDIS_URL | str | no | redis://localhost:6379/0 | Redis URL for the RQ worker queue. |
| ANTHROPIC_API_KEY | str | yes | (secret) | Anthropic key (claude-sonnet-4-5). |
| DEEPSEEK_API_KEY | str | yes | (secret) | DeepSeek key (deepseek-chat). |
| DEEPSEEK_BASE_URL | str | no | https://api.deepseek.com/v1 | DeepSeek endpoint. |
| FREELLMAPI_API_KEY | str | yes | (secret) | Self-hosted FreeLLMAPI key (http://localhost:3001). |
| FREELLMAPI_BASE_URL | str | no | http://localhost:3001/v1 | FreeLLMAPI endpoint. |
| GEMINI_API_KEY | str | yes | (secret) | Google AI Studio key — free Gemini flash. |
| GEMINI_BASE_URL | str | no | https://generativelanguage.googleapis.com/v1beta | Gemini endpoint (native API). |
| GROQ_API_KEY | str | yes | (secret) | Groq key — generous free tier (llama-3.3-70b). |
| GROQ_BASE_URL | str | no | https://api.groq.com/openai/v1 | Groq OpenAI-compatible endpoint. |
| HUGGINGFACE_API_KEY | str | yes | (secret) | HuggingFace token — serverless inference. |
| HUGGINGFACE_BASE_URL | str | no | https://router.huggingface.co/hf-inference/v1 | HuggingFace router endpoint. |
| MISTRAL_API_KEY | str | yes | (secret) | Mistral key — free open models. |
| MISTRAL_BASE_URL | str | no | https://api.mistral.ai/v1 | Mistral endpoint. |
| OLLAMA_BASE_URL | str | no | http://localhost:11434/v1 | Local Ollama endpoint (keyless). |
| OPENAI_API_KEY | str | yes | (secret) | OpenAI key (gpt-4o-mini). |
| OPENROUTER_API_KEY | str | yes | (secret) | OpenRouter key — many free :free models. |
| OPENROUTER_APP_NAME | str | no | Bob Agent | App name sent as OpenRouter X-Title. |
| OPENROUTER_BASE_URL | str | no | https://openrouter.ai/api/v1 | OpenRouter endpoint. |
| OPENROUTER_SITE_URL | str | no | https://localhost | Site URL sent as OpenRouter HTTP-Referer. |
| TOGETHER_API_KEY | str | yes | (secret) | Together AI key — free-tier turbo models. |
| TOGETHER_BASE_URL | str | no | https://api.together.xyz/v1 | Together AI endpoint. |
| TOKENROUTER_API_KEY | str | yes | (secret) | TokenRouter key (multi-provider gateway). |
| TOKENROUTER_BASE_URL | str | no | https://api.tokenrouter.io/v1 | TokenRouter endpoint. |
| DEFAULT_MODEL | str | no |  | Default model id (empty = provider default). |
| DEFAULT_PROVIDER | str | no | echo | Provider key used when no task rule matches (echo = offline). |
| PROVIDER_EXTRA_HEADERS | str | yes | (secret) | Extra headers for every provider call, as JSON. |
| AGENT_BOOTSTRAP_SECRET | str | yes | (secret) | Bootstrap secret accepted by POST /api/v1/auth/token. |
| API_SESSION_SECRET | str | yes | (secret) | Signs API tokens (minted via POST /api/v1/auth/token). |
| TELEGRAM_ALLOWED_CHAT_IDS | str | no |  | Comma-separated chat ids allowed to use the bot. |
| TELEGRAM_BOT_TOKEN | str | yes | (secret) | Bot token from @BotFather (empty = gateway off). |
| TELEGRAM_WEBHOOK_SECRET | str | yes | (secret) | Set = webhook mode; empty = polling mode. |
| DATABASE_URL | str | no | sqlite:///data/agent_system.db | SQLAlchemy URL (SQLite by default). |
| OUTPUTS_DIR | path | no | outputs | Generated file outputs. |
| RECORDINGS_DIR | path | no | recordings | Behavior recordings (.jsonl). |
| SKILLS_DIR | path | no | skills | Agent skills (SKILL.md folders). |
| SOUL_PATH | str | no |  | Agent identity file (empty = auto-discover SOUL.md). |
| TEMPLATES_DIR | path | no | templates | Workspace template tarballs. |
| VAULT_PATH | path | no | ~/Downloads/Claude memory | Obsidian memory vault directory. |
| WORKSPACES_DIR | path | no | workspaces | Coding workspace roots. |
| TOOLS_SHELL_MODE | str | no | sandbox | Shell policy: sandbox (Docker), local (host opt-in), off. |
| TOOLS_REQUIRE_APPROVAL | bool | no | true | Execute tools demand a live approval first. |
| TOOLS_MAX_ITERS | int | no | 8 | Max Reason+Act iterations per LLM task. |
| TOOLS_FS_ROOTS | str | no |  | Extra comma-separated roots for file tools (jail). |
| MEMORY_AUTO_REMEMBER | bool | no | true | Persist task outcomes to the Obsidian vault. |
| MEMORY_RECALL_TOP_K | int | no | 3 | Vault notes injected into LLM task prompts. |
| OPENCONNECTOR_BASE_URL | str | no |  | OpenConnector runtime URL (empty = tools hidden; docker: http://localhost:3000). |
| OPENCONNECTOR_RUNTIME_TOKEN | str | yes | (unset) | Runtime bearer token for /v1 + /mcp calls (optional on localhost). |
| OPENCONNECTOR_ADMIN_TOKEN | str | yes | (unset) | Admin token for action guides + console (optional). |
| OPENCONNECTOR_API_KEY | str | yes | (unset) | Back-compat alias of OPENCONNECTOR_RUNTIME_TOKEN. |
| OPENCONNECTOR_ALIAS | str | no |  | Named connection sent as x-oo-connector-alias (optional). |
| OPENCONNECTOR_USER_ID | str | no | bob-local | Deprecated — unused by the OpenConnector API. |
| MCP_SERVERS | str | no | [] | MCP servers as JSON list (stdio command or HTTP url + headers). |
| A2A_ENABLED | bool | no | false | Outbound agent delegation (off by default; needs per-target approval). |
| SCHEDULER_ENABLED | bool | no | true | Run the cron/interval scheduler in the API server. |
| SCHEDULER_TIMEZONE | str | no | UTC | Timezone for cron schedules. |
| DAILY_BUDGET_USD | float | no | 10.0 | Daily model-spend budget cap. |
| MAX_BROWSER_SESSIONS | int | no | 3 | Max parallel browser sessions. |
| MAX_CONCURRENT_AGENTS | int | no | 8 | Max parallel agent runs. |
| MAX_CONCURRENT_TASKS | int | no | 16 | Max parallel tasks. |
| MAX_CONTAINER_CPU | float | no | 2.0 | Max CPUs per sandbox container. |
| MAX_CONTAINER_MEMORY_MB | int | no | 2048 | Max RAM per sandbox container. |
| MAX_EXECUTION_TIME_SECONDS | int | no | 1800 | Max seconds per execution. |
| MAX_FILE_SIZE_MB | int | no | 10 | Max single file read/write. |
| MAX_LOG_SIZE_MB | int | no | 100 | Max log capture size. |
| MAX_OUTPUT_SIZE_MB | int | no | 50 | Max generated output size. |
| MAX_RETRIES | int | no | 3 | Max retries per task. |
| MAX_TASK_COST_USD | float | no | 2.0 | Max spend per task. |
| MAX_TASK_TOKENS | int | no | 200000 | Max tokens per task. |
| MAX_WORKSPACE_SIZE_MB | int | no | 512 | Max workspace size on disk. |

## Notes

- **Providers.** Any `*_API_KEY` activates its provider (Ollama needs none).
  `DEFAULT_PROVIDER=echo` (or unset) = offline mode. Test a key live:
  `POST /api/v1/model-routing/test {"provider": "groq"}`, `/model test` in
  the chat, or tick it in `make setup`. `PROVIDER_EXTRA_HEADERS` is treated
  as secret (it often carries keys) and quoted automatically in `.env.local`.
  Switch the default **live** (no restart) from the chat with
  `/model set <provider> [model]`.
- **Answer profiles.** Non-secret wizard answers persist in
  `~/.config/bob-agent/setup.json` (`--profile` / `BOB_PROFILE` override);
  re-runs offer reuse, and `setup --yes` is fully non-interactive.
- **Skills & soul config.** `SKILLS_DIR` points at skill packs;
  enable/config overrides live beside the skills (`.state.json`,
  `config.local.yaml`). `SOUL_PATH` empty means auto-discover `./SOUL.md`
  then parent-dir `SOUL.md` — from `backend/` that finds the repo-root file.

## Tools & ReAct

`TOOLS_SHELL_MODE` controls the shell tool: `sandbox` (default — runs in the
Docker sandbox), `local` (explicit host opt-in — never enable on an untrusted
machine), or `off` (tool hidden). `TOOLS_REQUIRE_APPROVAL=true` makes every
execute-tier tool request a persisted approval first — the ReAct loop then
tells the model *"ask the user to approve, then continue without re-running"*
(`${approval_id}` printed to the caller), and the task fails honestly until
approved + retried. `TOOLS_MAX_ITERS` caps Reason+Act iterations per task
(default 8). `TOOLS_FS_ROOTS` adds comma-separated extra jail roots for the
file tools (defaults: `workspaces/`, `outputs/`, repo root).

The agent tool registry (visible via `/tools` in the chat):

| Tool | Risk | Notes |
|---|---|---|
| `shell` | execute | sandboxed by default; approval-gated |
| `file_read` / `file_write` / `file_list` | read / write / read | jailed to allowed roots; secret paths refused |
| `web_fetch` | read | title + readable text extraction |
| `memory_recall` / `memory_remember` | read / write | Obsidian vault |
| `tasks_inspect` | read | recent tasks of a session |
| `openconnector_execute` / `openconnector_list` | execute / read | only when `OPENCONNECTOR_BASE_URL` set |
| `mcp_call` / `mcp_list` | execute / read | only when MCP servers configured |

## OpenConnector (SaaS connector gateway)

OpenConnector (`oomol-lab/open-connector`) is a self-hosted, open-source
Pipedream/Composio alternative — 1,000+ providers, 10,000+ prebuilt Actions
(GitHub, Gmail, Slack, Notion, …). Bob talks to it two ways:

1. **HTTP Runtime API** — `openconnector_execute` posts
   `POST /v1/actions/:actionId {input, connectionName?}`; `openconnector_list`
   discovers actions per service. Credentials never leave the gateway.
2. **Implicit MCP server** — when configured, an `openconnector` streamable-
   HTTP MCP server (`POST /mcp`) is auto-appended to `all_servers()`, so the
   native MCP tools (`list_apps`, `search_actions`, `get_action_guide`,
   `execute_action`) are available through `mcp_list`/`mcp_call` too.

Quick start:

```bash
docker compose up openconnector          # → http://localhost:3000
agentctl settings set OPENCONNECTOR_BASE_URL http://localhost:3000
agentctl settings set OPENCONNECTOR_RUNTIME_TOKEN your-runtime-token   # optional locally
# (or export OPENCONNECTOR_* env vars / add to .env.local)
agentctl chat   # → /tools   (shows the gateway + tools lit up)
```

Token model (from the OpenConnector docs):
- `OOMOL_CONNECT_RUNTIME_TOKEN` server-side = bootstrap runtime token for
  `/v1` + `/mcp`; mirror it client-side as `OPENCONNECTOR_RUNTIME_TOKEN`.
- `OOMOL_CONNECT_ADMIN_TOKEN` server-side = admin API (action guides, web
  console); mirror as `OPENCONNECTOR_ADMIN_TOKEN` for `get_action_guide`.
- Local runtimes need no token at all (unconfigured = tools stay hidden).
- `OPENCONNECTOR_ALIAS` (client) is sent as `x-oo-connector-alias` to select
  a named connection instead of the provider default.

Set `OOMOL_CONNECT_ALLOWED_ACTIONS` on the server to constrain what the
gateway may execute (e.g. `hackernews.*,github.get_current_user`).

## MCP servers

`MCP_SERVERS` is a JSON list with two entry shapes:

```jsonc
[
  // stdio server (command + args)
  {"name": "fs", "command": "npx",
   "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]},
  // streamable-HTTP server (url + optional headers)
  {"name": "remote", "url": "https://host/mcp",
   "headers": {"Authorization": "Bearer …"}}
]
```

Set it live from the chat: `/settings set MCP_SERVERS '<json>'`, then
`/tools` lists the servers. Session handling: stdio spawns a fresh process
per call; HTTP creates a session, replays `Mcp-Session-Id`, and closes after
the call. An explicit entry named `openconnector` overrides the implicit
OpenConnector server.

### Shipped server: the Obsidian vault (`bob-vault-mcp`)

Bob ships one MCP server of its own — `bob-vault-mcp` — so the vault keeps
updating itself without a bespoke code path. Attach it like any other server:

```jsonc
[{"name": "vault", "command": "uv", "args": ["run", "bob-vault-mcp"],
  "cwd": "agent-system/backend"}]
```

It exposes six tools (all reached through `mcp_list`/`mcp_call`, so every call
is approval-gated on `mcp:vault:<tool>` and audit-logged):

| Tool | What it does |
|---|---|
| `vault_write_note` | Write one memory-layer note (frontmatter + wiki-links) |
| `vault_append_daily` | Append a timestamped line to `daily/<YYYY-MM-DD>.md` |
| `vault_record` | Update the dedicated **Bob Agent record** (`records/bob-agent.md`) |
| `vault_read_record` | Read that record: counters + newest entries |
| `vault_recall` | Keyword-rank the vault's memory notes |
| `vault_status` | Vault root, per-layer counts, record path |

Every write reuses the vault contract already used by the memory subsystem
(write-time secret scrubbing, the note-size bound, atomic replace), so the MCP
path and the internal path cannot drift. The vault root comes from `VAULT_PATH`;
run the server standalone with `uv run bob-vault-mcp --help`.

## Scheduler

`SCHEDULER_ENABLED=true` runs the APScheduler loop inside the API server;
`SCHEDULER_TIMEZONE` (default `UTC`) applies to cron expressions. Jobs are
created via `POST /api/v1/schedule` (`{"goal": …, "schedule": "cron|interval"}`),
listed via `GET /api/v1/schedule`, removed via `DELETE /api/v1/schedule/{id}`,
and managed from the chat with `/schedule [rm <id>]`.
