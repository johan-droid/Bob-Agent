# 📚 Bob Agent Memory Vault

Welcome to the dedicated Obsidian Memory Vault for **Bob Agent**.

This vault serves as Bob's long-term episodic and semantic memory storage.

---

## Vault Structure

- `memories/` — Synthesized agent reflections, key decisions, and persistent user preferences.
- `tasks/` — Task execution summaries, outcomes, and learnings.
- `daily/` — Daily logs and agent insight digests.
- `knowledge/` — Ingested documents, architectural notes, and project reference materials.
- `records/` — Long-lived records. `records/bob-agent.md` is the dedicated Bob
  Agent record: one entry per recorded event, updated in place by the
  `bob-vault-mcp` MCP server.

---

## Keeping the vault updated

Bob writes memory notes on its own, and `bob-vault-mcp` (attached through
`MCP_SERVERS`) adds an explicit, agent-callable path:

| Tool | Writes |
|---|---|
| `vault_write_note` | `memory/<layer>/…` notes |
| `vault_append_daily` | `daily/<YYYY-MM-DD>.md` |
| `vault_record` | `records/bob-agent.md` (the separate Bob Agent record) |

Every write is secret-scrubbed before it lands, and each MCP call needs approval
(`mcp:vault:<tool>`).

---

*Managed automatically by Bob Agent v3.1 Memory Subsystem.*
