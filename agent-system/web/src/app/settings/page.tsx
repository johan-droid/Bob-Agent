"use client";

import { useEffect, useState, useCallback } from "react";
import { api, type SettingsGroupOut, type ModelRoutingProviders } from "@/lib/api";
import { Skeleton } from "@/components/Skeleton";
import { ErrorBanner } from "@/components/ErrorBanner";
import { useToast } from "@/components/Toast";

const TABS = [
  { id: "providers", label: "⚡ LLM Providers", icon: "⚡" },
  { id: "tools", label: "🔧 Tools & Sandbox", icon: "🔧" },
  { id: "memory", label: "📚 Obsidian Memory", icon: "📚" },
  { id: "cost", label: "💰 Cost Limits", icon: "💰" },
  { id: "resources", label: "⚙️ Resource Limits", icon: "⚙️" },
  { id: "telegram", label: "📱 Telegram", icon: "📱" },
  { id: "integrations", label: "🔌 MCP & Connectors", icon: "🔌" },
];

const PROVIDER_INFO: Record<string, { label: string; placeholder: string; docUrl?: string }> = {
  anthropic: { label: "Anthropic Claude", placeholder: "sk-ant-api..." },
  openai: { label: "OpenAI", placeholder: "sk-proj-..." },
  groq: { label: "Groq LPU", placeholder: "gsk_..." },
  ollama: { label: "Ollama (Local)", placeholder: "http://localhost:11434" },
  openrouter: { label: "OpenRouter", placeholder: "sk-or-v1-..." },
  together: { label: "Together AI", placeholder: "tog_..." },
  mistral: { label: "Mistral AI", placeholder: "..." },
  gemini: { label: "Google Gemini", placeholder: "AIzaSy..." },
  deepseek: { label: "DeepSeek", placeholder: "sk-..." },
  huggingface: { label: "HuggingFace", placeholder: "hf_..." },
  freellmapi: { label: "FreeLLMAPI", placeholder: "..." },
  tokenrouter: { label: "TokenRouter", placeholder: "..." },
};

export default function SettingsPage() {
  const [activeTab, setActiveTab] = useState("providers");
  const [settings, setSettings] = useState<SettingsGroupOut[]>([]);
  const [routing, setRouting] = useState<ModelRoutingProviders | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, { status: "testing" | "ok" | "failed"; msg: string }>>({});
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const toast = useToast();

  const load = useCallback(async () => {
    try {
      const [s, r] = await Promise.all([
        api.listSettings().catch(() => [] as SettingsGroupOut[]),
        api.listRoutingProviders().catch(() => null),
      ]);
      setSettings(s);
      setRouting(r);
      setLoadError(null);
    } catch (err) {
      setLoadError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const getSetting = useCallback(
    (key: string): string => {
      for (const group of settings) {
        const found = group.settings.find((s) => s.key === key);
        if (found) return found.value;
      }
      return "";
    },
    [settings],
  );

  const handleSave = useCallback(
    async (key: string, value: string) => {
      setSaving(key);
      setFieldErrors((prev) => {
        const next = { ...prev };
        delete next[key];
        return next;
      });
      try {
        await api.setSetting(key, value);
        setSettings(await api.listSettings());
        toast(`Saved setting: ${key}`, "success");
      } catch (err) {
        const msg = (err as Error).message;
        setFieldErrors((prev) => ({ ...prev, [key]: msg }));
        toast(`Failed to save ${key}: ${msg}`, "error");
      } finally {
        setSaving(null);
      }
    },
    [toast],
  );

  const handleTestProvider = useCallback(async (provider: string) => {
    setTestResults((prev) => ({
      ...prev,
      [provider]: { status: "testing", msg: "Testing connection…" },
    }));
    try {
      const result = await api.testModelProvider(provider);
      setTestResults((prev) => ({
        ...prev,
        [provider]: {
          status: result.ok ? "ok" : "failed",
          msg: result.ok
            ? `✓ Connected (${result.latency_ms ?? 0}ms)`
            : `✕ Error: ${result.error || "failed"}`,
        },
      }));
    } catch (err) {
      setTestResults((prev) => ({
        ...prev,
        [provider]: { status: "failed", msg: `✕ Error: ${(err as Error).message}` },
      }));
    }
  }, []);

  if (loading) {
    return (
      <div className="settings-container">
        <h1 className="page-title">Agent Settings & Configuration</h1>
        <Skeleton lines={8} />
      </div>
    );
  }

  return (
    <div className="settings-layout">
      <div className="settings-header-banner">
        <div>
          <h1 className="page-title">System Settings</h1>
          <p className="page-subtitle">
            Configure LLM providers, tool execution sandboxes, Obsidian memory, cost guardrails, and integrations.
          </p>
        </div>
      </div>

      {loadError && <ErrorBanner message={loadError} onRetry={() => void load()} />}

      <div className="settings-tabs" role="tablist" aria-label="Settings sections">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            role="tab"
            aria-selected={activeTab === tab.id}
            className={`settings-tab${activeTab === tab.id ? " active" : ""}`}
            onClick={() => setActiveTab(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div className="settings-tab-content">
        {activeTab === "providers" && (
          <ProvidersTab
            routing={routing}
            getSetting={getSetting}
            onSave={handleSave}
            saving={saving}
            onTest={handleTestProvider}
            testResults={testResults}
            fieldErrors={fieldErrors}
          />
        )}
        {activeTab === "tools" && (
          <ToolsTab
            getSetting={getSetting}
            onSave={handleSave}
            saving={saving}
            fieldErrors={fieldErrors}
          />
        )}
        {activeTab === "memory" && (
          <MemoryTab
            getSetting={getSetting}
            onSave={handleSave}
            saving={saving}
            fieldErrors={fieldErrors}
          />
        )}
        {activeTab === "cost" && (
          <CostTab
            getSetting={getSetting}
            onSave={handleSave}
            saving={saving}
            fieldErrors={fieldErrors}
          />
        )}
        {activeTab === "resources" && (
          <ResourcesTab
            getSetting={getSetting}
            onSave={handleSave}
            saving={saving}
            fieldErrors={fieldErrors}
          />
        )}
        {activeTab === "telegram" && (
          <TelegramTab
            getSetting={getSetting}
            onSave={handleSave}
            saving={saving}
            fieldErrors={fieldErrors}
          />
        )}
        {activeTab === "integrations" && (
          <IntegrationsTab
            getSetting={getSetting}
            onSave={handleSave}
            saving={saving}
          />
        )}
      </div>
    </div>
  );
}

interface TabProps {
  getSetting: (k: string) => string;
  onSave: (k: string, v: string) => void;
  saving: string | null;
  fieldErrors: Record<string, string>;
}

function SettingField({
  label,
  help,
  value,
  saving,
  onSave,
  type = "text",
  placeholder,
  error,
}: {
  label: string;
  help?: string;
  value: string;
  saving: string | null;
  onSave: (val: string) => void;
  type?: string;
  placeholder?: string;
  error?: string;
}) {
  const [local, setLocal] = useState(value);
  useEffect(() => {
    setLocal(value);
  }, [value]);
  const id = `setting-${label}`;

  return (
    <div className="setting-row">
      <label className="setting-label" htmlFor={id}>
        {label}
      </label>
      <div className="setting-field-wrap">
        <input
          id={id}
          className="setting-input"
          type={type}
          value={local}
          placeholder={placeholder}
          onChange={(e) => setLocal(e.target.value)}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? `${id}-error` : undefined}
        />
        {help && <div className="setting-help">{help}</div>}
        {error && (
          <div id={`${id}-error`} className="field-error" role="alert">
            Save failed: {error}
          </div>
        )}
      </div>
      <button
        type="button"
        className="btn btn-save"
        disabled={saving === label || local === value}
        onClick={() => onSave(local)}
      >
        {saving === label ? "Saving…" : "Save"}
      </button>
    </div>
  );
}

function ProvidersTab({
  routing,
  getSetting,
  onSave,
  saving,
  onTest,
  testResults,
  fieldErrors,
}: {
  routing: ModelRoutingProviders | null;
  getSetting: (key: string) => string;
  onSave: (key: string, val: string) => void;
  saving: string | null;
  onTest: (provider: string) => void;
  testResults: Record<string, { status: "testing" | "ok" | "failed"; msg: string }>;
  fieldErrors: Record<string, string>;
}) {
  return (
    <div className="settings-section-stack">
      <div className="card">
        <div className="card-header-with-badge">
          <h3>Active Routing & Defaults</h3>
          {routing && (
            <span className="status-pill status-active">
              Router: {routing.router_active ? "Active" : "Standby"}
            </span>
          )}
        </div>
        <p className="muted">
          Current model routing uses: <strong>{routing?.default_provider || getSetting("DEFAULT_PROVIDER") || "echo"}</strong> / <strong>{routing?.default_model || getSetting("DEFAULT_MODEL") || "default"}</strong>
        </p>
        <SettingField
          label="DEFAULT_PROVIDER"
          help="Which provider to use by default (e.g. anthropic, openai, groq, gemini, ollama, echo)"
          value={getSetting("DEFAULT_PROVIDER") || "echo"}
          saving={saving}
          onSave={(v) => onSave("DEFAULT_PROVIDER", v)}
          error={fieldErrors["DEFAULT_PROVIDER"]}
        />
        <SettingField
          label="DEFAULT_MODEL"
          help="Model identifier (e.g. claude-3-7-sonnet-20250219, gpt-4o, llama-3.3-70b-versatile)"
          value={getSetting("DEFAULT_MODEL")}
          saving={saving}
          onSave={(v) => onSave("DEFAULT_MODEL", v)}
          placeholder="Leave empty for provider default"
          error={fieldErrors["DEFAULT_MODEL"]}
        />
      </div>

      <div className="card">
        <h3>Provider Credentials & Connectivity</h3>
        <p className="muted">
          Configure API keys. Test each connection live to check credentials and roundtrip latency.
        </p>

        <div className="providers-grid">
          {Object.entries(PROVIDER_INFO).map(([key, info]) => {
            const keyName = `${key.toUpperCase()}_API_KEY`;
            const baseUrlKey = `${key.toUpperCase()}_BASE_URL`;
            const hasKey = getSetting(keyName);
            const test = testResults[key];

            return (
              <div key={key} className="provider-card">
                <div className="provider-card-header">
                  <div>
                    <h4>{info.label}</h4>
                    <span className="provider-sublabel">ID: {key}</span>
                  </div>
                  <div className="provider-badges">
                    {hasKey ? (
                      <span className="provider-badge configured">Configured</span>
                    ) : (
                      <span className="provider-badge free">Echo Mode</span>
                    )}
                    <button
                      type="button"
                      className="btn secondary btn-sm"
                      onClick={() => onTest(key)}
                      disabled={test?.status === "testing"}
                    >
                      {test?.status === "testing" ? "Testing…" : "🧪 Test"}
                    </button>
                  </div>
                </div>

                {test && (
                  <div className={`provider-test-pill test-${test.status}`}>
                    {test.msg}
                  </div>
                )}

                <SettingField
                  label={keyName}
                  help="API Key"
                  value={hasKey ? "••••••••••••" : ""}
                  saving={saving}
                  onSave={(v) => onSave(keyName, v)}
                  type="password"
                  placeholder={info.placeholder}
                  error={fieldErrors[keyName]}
                />
                <SettingField
                  label={baseUrlKey}
                  help="Custom base URL (Optional)"
                  value={getSetting(baseUrlKey)}
                  saving={saving}
                  onSave={(v) => onSave(baseUrlKey, v)}
                  placeholder="Default endpoint"
                  error={fieldErrors[baseUrlKey]}
                />
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function ToolsTab({ getSetting, onSave, saving, fieldErrors }: TabProps) {
  return (
    <div className="card">
      <h3>Agent Tools & Sandbox Execution</h3>
      <p className="muted">Configure execution security, tool approval gates, and iteration limits.</p>
      <SettingField
        label="TOOLS_SHELL_MODE"
        help="sandbox (Docker container), local (host machine), or off (disabled)"
        value={getSetting("TOOLS_SHELL_MODE") || "sandbox"}
        saving={saving}
        onSave={(v) => onSave("TOOLS_SHELL_MODE", v)}
        error={fieldErrors["TOOLS_SHELL_MODE"]}
      />
      <SettingField
        label="TOOLS_REQUIRE_APPROVAL"
        help="Require human authorization before running dangerous tools (true/false)"
        value={getSetting("TOOLS_REQUIRE_APPROVAL") || "true"}
        saving={saving}
        onSave={(v) => onSave("TOOLS_REQUIRE_APPROVAL", v)}
        error={fieldErrors["TOOLS_REQUIRE_APPROVAL"]}
      />
      <SettingField
        label="TOOLS_MAX_ITERS"
        help="Maximum ReAct reasoning and tool call iterations per task"
        value={getSetting("TOOLS_MAX_ITERS") || "8"}
        saving={saving}
        onSave={(v) => onSave("TOOLS_MAX_ITERS", v)}
        error={fieldErrors["TOOLS_MAX_ITERS"]}
      />
      <SettingField
        label="TOOLS_FS_ROOTS"
        help="Allowed filesystem boundary roots (comma-separated)"
        value={getSetting("TOOLS_FS_ROOTS")}
        saving={saving}
        onSave={(v) => onSave("TOOLS_FS_ROOTS", v)}
        error={fieldErrors["TOOLS_FS_ROOTS"]}
      />
      <SettingField
        label="TOOLS_PLUGIN_DIR"
        help="Custom directory for dynamic tool plugins"
        value={getSetting("TOOLS_PLUGIN_DIR") || "tools_plugins"}
        saving={saving}
        onSave={(v) => onSave("TOOLS_PLUGIN_DIR", v)}
        error={fieldErrors["TOOLS_PLUGIN_DIR"]}
      />
    </div>
  );
}

function MemoryTab({ getSetting, onSave, saving, fieldErrors }: TabProps) {
  return (
    <div className="card">
      <h3>Obsidian Memory Vault & Embeddings</h3>
      <p className="muted">Control episodic and semantic memory indexing in your Obsidian vault.</p>
      <SettingField
        label="VAULT_PATH"
        help="Absolute path to your local Obsidian vault directory"
        value={getSetting("VAULT_PATH")}
        saving={saving}
        onSave={(v) => onSave("VAULT_PATH", v)}
        placeholder="/home/user/ObsidianVault"
        error={fieldErrors["VAULT_PATH"]}
      />
      <SettingField
        label="MEMORY_AUTO_REMEMBER"
        help="Automatically synthesize and record session outcomes to Obsidian (true/false)"
        value={getSetting("MEMORY_AUTO_REMEMBER") || "true"}
        saving={saving}
        onSave={(v) => onSave("MEMORY_AUTO_REMEMBER", v)}
        error={fieldErrors["MEMORY_AUTO_REMEMBER"]}
      />
      <SettingField
        label="MEMORY_RECALL_TOP_K"
        help="Number of relevant memory notes injected into agent prompt context"
        value={getSetting("MEMORY_RECALL_TOP_K") || "3"}
        saving={saving}
        onSave={(v) => onSave("MEMORY_RECALL_TOP_K", v)}
        error={fieldErrors["MEMORY_RECALL_TOP_K"]}
      />
      <SettingField
        label="MEMORY_EMBEDDING_PROVIDER"
        help="hash (offline fast hashing) or local (sentence-transformers embeddings)"
        value={getSetting("MEMORY_EMBEDDING_PROVIDER") || "hash"}
        saving={saving}
        onSave={(v) => onSave("MEMORY_EMBEDDING_PROVIDER", v)}
        error={fieldErrors["MEMORY_EMBEDDING_PROVIDER"]}
      />
    </div>
  );
}

function CostTab({ getSetting, onSave, saving, fieldErrors }: TabProps) {
  return (
    <div className="card">
      <h3>Cost & Budget Guardrails</h3>
      <p className="muted">Set hard and soft financial boundaries to prevent unexpected model spend.</p>
      <SettingField
        label="DAILY_BUDGET_USD"
        help="Maximum total spend in USD across all providers per calendar day"
        value={getSetting("DAILY_BUDGET_USD") || "10.0"}
        saving={saving}
        onSave={(v) => onSave("DAILY_BUDGET_USD", v)}
        error={fieldErrors["DAILY_BUDGET_USD"]}
      />
      <SettingField
        label="MAX_TASK_COST_USD"
        help="Maximum spend cap for a single task execution"
        value={getSetting("MAX_TASK_COST_USD") || "2.0"}
        saving={saving}
        onSave={(v) => onSave("MAX_TASK_COST_USD", v)}
        error={fieldErrors["MAX_TASK_COST_USD"]}
      />
      <SettingField
        label="MAX_TASK_TOKENS"
        help="Maximum combined input + output tokens per task"
        value={getSetting("MAX_TASK_TOKENS") || "200000"}
        saving={saving}
        onSave={(v) => onSave("MAX_TASK_TOKENS", v)}
        error={fieldErrors["MAX_TASK_TOKENS"]}
      />
    </div>
  );
}

function ResourcesTab({ getSetting, onSave, saving, fieldErrors }: TabProps) {
  return (
    <div className="card">
      <h3>Concurrency & Context Limits</h3>
      <SettingField
        label="MAX_CONCURRENT_AGENTS"
        help="Maximum concurrent agent runs allowed"
        value={getSetting("MAX_CONCURRENT_AGENTS") || "8"}
        saving={saving}
        onSave={(v) => onSave("MAX_CONCURRENT_AGENTS", v)}
        error={fieldErrors["MAX_CONCURRENT_AGENTS"]}
      />
      <SettingField
        label="MAX_CONCURRENT_TASKS"
        help="Maximum concurrent tasks allowed"
        value={getSetting("MAX_CONCURRENT_TASKS") || "16"}
        saving={saving}
        onSave={(v) => onSave("MAX_CONCURRENT_TASKS", v)}
        error={fieldErrors["MAX_CONCURRENT_TASKS"]}
      />
      <SettingField
        label="MAX_WORKSPACE_SIZE_MB"
        help="Maximum allowed workspace directory size in MB"
        value={getSetting("MAX_WORKSPACE_SIZE_MB") || "512"}
        saving={saving}
        onSave={(v) => onSave("MAX_WORKSPACE_SIZE_MB", v)}
        error={fieldErrors["MAX_WORKSPACE_SIZE_MB"]}
      />
      <SettingField
        label="MAX_CONTEXT_TOKENS"
        help="Maximum context window tokens before compression triggers"
        value={getSetting("MAX_CONTEXT_TOKENS") || "100000"}
        saving={saving}
        onSave={(v) => onSave("MAX_CONTEXT_TOKENS", v)}
        error={fieldErrors["MAX_CONTEXT_TOKENS"]}
      />
      <SettingField
        label="CONTEXT_COMPACTION_THRESHOLD_PCT"
        help="When to compact history (% of maximum context window)"
        value={getSetting("CONTEXT_COMPACTION_THRESHOLD_PCT") || "75.0"}
        saving={saving}
        onSave={(v) => onSave("CONTEXT_COMPACTION_THRESHOLD_PCT", v)}
        error={fieldErrors["CONTEXT_COMPACTION_THRESHOLD_PCT"]}
      />
    </div>
  );
}

function TelegramTab({ getSetting, onSave, saving, fieldErrors }: TabProps) {
  return (
    <div className="card">
      <h3>Telegram Bot Gateway</h3>
      <p className="muted">Allow Bob to receive and execute commands from authorized Telegram users.</p>
      <SettingField
        label="TELEGRAM_BOT_TOKEN"
        help="Bot API Token issued by @BotFather"
        value={getSetting("TELEGRAM_BOT_TOKEN") ? "••••••••" : ""}
        saving={saving}
        onSave={(v) => onSave("TELEGRAM_BOT_TOKEN", v)}
        type="password"
        placeholder="123456789:ABCdef..."
        error={fieldErrors["TELEGRAM_BOT_TOKEN"]}
      />
      <SettingField
        label="TELEGRAM_ALLOWED_CHAT_IDS"
        help="Comma-separated list of authorized chat or user IDs"
        value={getSetting("TELEGRAM_ALLOWED_CHAT_IDS")}
        saving={saving}
        onSave={(v) => onSave("TELEGRAM_ALLOWED_CHAT_IDS", v)}
        placeholder="12345678, 87654321"
        error={fieldErrors["TELEGRAM_ALLOWED_CHAT_IDS"]}
      />
    </div>
  );
}

function IntegrationsTab({
  getSetting,
  onSave,
  saving,
}: {
  getSetting: (k: string) => string;
  onSave: (k: string, v: string) => void;
  saving: string | null;
}) {
  const stored = getSetting("MCP_SERVERS");
  const [mcpJson, setMcpJson] = useState("{}");
  const [mcpError, setMcpError] = useState<string | null>(null);

  useEffect(() => {
    try {
      setMcpJson(JSON.stringify(JSON.parse(stored || "{}"), null, 2));
      setMcpError(null);
    } catch {
      setMcpJson(stored || "{}");
    }
  }, [stored]);

  const loadMcpTemplate = (templateKey: string) => {
    let sample = {};
    if (templateKey === "brave") {
      sample = {
        brave_search: {
          command: "npx",
          args: ["-y", "@modelcontextprotocol/server-brave-search"],
          env: { BRAVE_API_KEY: "YOUR_KEY_HERE" },
        },
      };
    } else if (templateKey === "filesystem") {
      sample = {
        filesystem: {
          command: "npx",
          args: ["-y", "@modelcontextprotocol/server-filesystem", "/home/user/projects"],
        },
      };
    }
    setMcpJson(JSON.stringify(sample, null, 2));
  };

  return (
    <div className="settings-section-stack">
      <div className="card">
        <h3>Model Context Protocol (MCP) Servers</h3>
        <p className="muted">
          Configure external tool servers using the standard MCP JSON schema (stdio & SSE).
        </p>

        <div className="mcp-template-buttons">
          <span>Templates:</span>
          <button
            type="button"
            className="btn btn-sm btn-secondary"
            onClick={() => loadMcpTemplate("brave")}
          >
            + Brave Search MCP
          </button>
          <button
            type="button"
            className="btn btn-sm btn-secondary"
            onClick={() => loadMcpTemplate("filesystem")}
          >
            + Filesystem MCP
          </button>
        </div>

        <textarea
          id="mcp-json"
          className="json-editor"
          value={mcpJson}
          onChange={(e) => setMcpJson(e.target.value)}
          aria-invalid={mcpError ? true : undefined}
          aria-describedby={mcpError ? "mcp-json-error" : undefined}
          rows={10}
        />
        {mcpError && (
          <p id="mcp-json-error" className="field-error" role="alert">
            {mcpError}
          </p>
        )}
        <div className="stack-8">
          <button
            type="button"
            className="btn btn-save"
            onClick={() => {
              try {
                const parsed = JSON.parse(mcpJson) as unknown;
                setMcpError(null);
                onSave("MCP_SERVERS", JSON.stringify(parsed));
              } catch {
                setMcpError("Invalid JSON: Check brackets, quotes, and commas.");
              }
            }}
          >
            {saving === "MCP_SERVERS" ? "Saving…" : "Save MCP Configuration"}
          </button>
        </div>
      </div>

      <div className="card">
        <h3>OpenConnector Gateway</h3>
        <SettingField
          label="OPENCONNECTOR_BASE_URL"
          help="Self-hosted connector gateway endpoint URL"
          value={getSetting("OPENCONNECTOR_BASE_URL")}
          saving={saving}
          onSave={(v) => onSave("OPENCONNECTOR_BASE_URL", v)}
          placeholder="http://localhost:3000"
        />
        <SettingField
          label="OPENCONNECTOR_RUNTIME_TOKEN"
          help="Authentication token for OpenConnector"
          value={getSetting("OPENCONNECTOR_RUNTIME_TOKEN") ? "••••••••" : ""}
          saving={saving}
          onSave={(v) => onSave("OPENCONNECTOR_RUNTIME_TOKEN", v)}
          type="password"
          placeholder="Enter token..."
        />
      </div>
    </div>
  );
}
