/**
 * Shared typed API client for the dashboard.
 *
 * Every call targets the real backend `/api/v1` (v3.1 §33 — no mocked
 * production paths). Event polling implements resume-from-sequence per
 * [[25_Event_System]] so reconnects lose nothing.
 */

export const API_BASE = "/api/v1";

export type Session = { id: string; goal: string; status: string };
export type Task = {
  id: string;
  session_id: string;
  task_type: string;
  title: string;
  state: string;
  agent_type: string | null;
  depends_on: string[];
  attempt: number;
  last_error: string | null;
};
export type Approval = {
  approval_id: string;
  requested_action: string;
  risk: string;
  scope: string;
  requester: string;
  decision: string;
  task_id: string | null;
  context: Record<string, unknown>;
  reason: string | null;
};
export type WorkspaceOut = {
  id: string;
  name: string;
  status: string;
  size_bytes: number;
  file_count: number;
};
export type EventOut = {
  event_id: string;
  sequence: number;
  timestamp: string;
  type: string;
  actor: string;
  session_id: string | null;
  task_id: string | null;
  agent_run_id: string | null;
  payload: Record<string, unknown>;
};
export type Artifact = {
  id: string;
  task_id: string | null;
  kind: string;
  path: string;
  size_bytes: number;
};
export type Recording = {
  recording_id: string;
  session_id: string | null;
  agent_run_id: string | null;
  action_count: number;
  started: string;
  finished: string | null;
};
export type ReplayStep = {
  index: number;
  timestamp: string;
  kind: string;
  name: string;
  payload: Record<string, unknown>;
};
export type ReplayResult = {
  recording_id: string;
  mode: string;
  allowed: boolean;
  blocked_reason: string | null;
  fingerprint_match: boolean;
  fingerprint_diff: Record<string, [unknown, unknown]>;
  steps: ReplayStep[];
  side_effects: number;
};
export type Recipe = {
  recipe_id: string;
  name: string;
  description: string;
  version: number;
  steps: number;
  parameters: Record<string, unknown>;
  tags: string[];
  executions: number;
};
export type Insight = {
  insight_id: string;
  insight_type: string;
  generated_at: string;
  key_findings: { finding: string; kind: string; suggested_action?: string }[];
  content: string;
  archived: boolean;
};
export type ModelCall = {
  id: string;
  task_id: string | null;
  provider: string;
  model_id: string;
  status: string;
  tokens_in: number | null;
  tokens_out: number | null;
  tokens_cached: number | null;
  usage_is_estimated: boolean;
  cost_usd: number | null;
  cost_is_estimated: boolean;
  latency_ms: number | null;
};
export type ScheduledJob = {
  job_id: string;
  name: string;
  kind: string;
  schedule: Record<string, unknown>;
  payload: Record<string, unknown>;
  enabled: boolean;
};
export type SettingOut = {
  key: string;
  value: string;
  source: string;
  group: string;
  help: string;
  secret: boolean;
};
export type SettingsGroupOut = {
  group: string;
  settings: SettingOut[];
};
export type ModelRoutingProviders = {
  default_provider: string;
  default_model: string;
  providers: Record<string, unknown>;
  pricing: Record<string, unknown>;
  router_active: boolean;
};
export type VaultNote = {
  name: string;
  path: string;
  layer: "SYSTEM" | "USER" | "TASK" | "WORKSPACE" | string;
  title: string;
  source: string;
  created: string;
  tags: string[];
  links: string[];
  size_bytes: number;
  task_id: string | null;
  session_id: string | null;
};
export type VaultNoteDetail = VaultNote & {
  body: string;
};
export type Template = {
  template_id: string;
  name: string;
  size_bytes: number;
  created_at: string;
  skipped_secrets: string[];
};

export type ProviderTestResult = {
  provider: string;
  model: string | null;
  ok: boolean;
  error: string | null;
  latency_ms: number | null;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    cache: "no-store",
    ...init,
  });
  if (!resp.ok) {
    const detail = await resp.text();
    throw new Error(`API ${resp.status}: ${detail}`);
  }
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

export const api = {
  // sessions
  listSessions: () => request<Session[]>("/sessions"),
  getSession: (id: string) => request<Session>(`/sessions/${id}`),
  createSession: (goal: string) =>
    request<Session>("/sessions", { method: "POST", body: JSON.stringify({ goal }) }),
  updateSession: (id: string, update: { goal?: string; status?: string }) =>
    request<Session>(`/sessions/${id}`, { method: "PATCH", body: JSON.stringify(update) }),
  deleteSession: (id: string) =>
    request<void>(`/sessions/${id}`, { method: "DELETE" }),
  // tasks
  listTasks: (sessionId?: string) =>
    request<Task[]>(`/tasks${sessionId ? `?session_id=${sessionId}` : ""}`),
  createTask: (sessionId: string, title: string, taskType = "general", input: Record<string, unknown> = {}) =>
    request<Task>("/tasks", {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId, title, task_type: taskType, input }),
    }),
  retryTask: (id: string) => request<Task>(`/tasks/${id}/retry`, { method: "POST" }),
  cancelTask: (id: string, reason = "user cancelled") =>
    request<Task>(`/tasks/${id}/transition`, {
      method: "POST",
      body: JSON.stringify({ target: "CANCELLED", reason }),
    }),
  transitionTask: (id: string, target: string, reason?: string) =>
    request<Task>(`/tasks/${id}/transition`, {
      method: "POST",
      body: JSON.stringify({ target, reason: reason ?? null }),
    }),
  // approvals
  listApprovals: (pendingOnly = true) =>
    request<Approval[]>(`/approvals?pending_only=${pendingOnly}`),
  decideApproval: (id: string, approve: boolean, policy = "ALLOW_ONCE") =>
    request<Approval>(`/approvals/${id}/decision`, {
      method: "POST",
      body: JSON.stringify({ approve, policy }),
    }),
  sweepApprovals: () => request<{ expired: string[] }>("/approvals/sweep", { method: "POST" }),
  // workspaces
  listWorkspaces: () => request<WorkspaceOut[]>("/workspaces"),
  createWorkspace: (name: string) =>
    request<WorkspaceOut>("/workspaces", { method: "POST", body: JSON.stringify({ name }) }),
  workspaceTree: (id: string) =>
    request<{ workspace_id: string; files: string[] }>(`/workspaces/${id}/tree`),
  readWorkspaceFile: (id: string, path: string) =>
    request<{ path: string; size: number; content_b64: string }>(
      `/workspaces/${id}/file?path=${encodeURIComponent(path)}`,
    ),
  writeWorkspaceFile: (id: string, path: string, content_b64: string) =>
    request<{ path: string; bytes: number }>(`/workspaces/${id}/file`, {
      method: "PUT",
      body: JSON.stringify({ path, content_b64 }),
    }),
  deleteWorkspace: (id: string) =>
    request<void>(`/workspaces/${id}`, { method: "DELETE" }),
  // templates (Phase 14)
  listTemplates: () => request<Template[]>("/templates"),
  createTemplate: (workspaceId: string, name: string) =>
    request<Template>("/templates", {
      method: "POST",
      body: JSON.stringify({ workspace_id: workspaceId, name }),
    }),
  restoreTemplate: (templateId: string, workspaceName?: string) =>
    request<WorkspaceOut>(`/templates/${templateId}/restore`, {
      method: "POST",
      body: JSON.stringify({ workspace_name: workspaceName ?? null }),
    }),
  deleteTemplate: (templateId: string) =>
    request<void>(`/templates/${templateId}`, { method: "DELETE" }),
  // vault / memory (Phase 8)
  listVaultNotes: (layer?: string, search?: string) =>
    request<VaultNote[]>(
      `/vault/notes?${layer ? `layer=${encodeURIComponent(layer)}&` : ""}${search ? `search=${encodeURIComponent(search)}` : ""}`,
    ),
  getVaultNote: (path: string) =>
    request<VaultNoteDetail>(`/vault/note?path=${encodeURIComponent(path)}`),
  createVaultNote: (note: {
    title: string;
    layer?: string;
    source?: string;
    body: string;
    tags?: string[];
    links?: string[];
    task_id?: string;
    session_id?: string;
  }) =>
    request<VaultNoteDetail>("/vault/notes", {
      method: "POST",
      body: JSON.stringify(note),
    }),
  deleteVaultNote: (path: string) =>
    request<void>(`/vault/notes?path=${encodeURIComponent(path)}`, { method: "DELETE" }),
  // artifacts
  listArtifacts: () => request<Artifact[]>("/artifacts"),
  getArtifact: (id: string) => request<Artifact>(`/artifacts/${id}`),
  // recordings + replay (Phase 15)
  listRecordings: () => request<Recording[]>("/recordings"),
  replay: (id: string, mode: string, context: Record<string, unknown> = {}, approvalId?: string) =>
    request<ReplayResult>(`/recordings/${id}/replay`, {
      method: "POST",
      body: JSON.stringify({ mode, context, approval_id: approvalId ?? null }),
    }),
  // recipes (Phase 16)
  listRecipes: () => request<Recipe[]>("/recipes"),
  getRecipe: (id: string) => request<Recipe>(`/recipes/${id}`),
  executeRecipe: (id: string, params: Record<string, unknown> = {}) =>
    request<{ recipe_id: string; session_id: string; task_ids: string[] }>(
      `/recipes/${id}/execute`,
      { method: "POST", body: JSON.stringify({ params }) },
    ),
  cancelRecipe: (id: string) =>
    request<Record<string, unknown>>(`/recipes/${id}/cancel`, { method: "POST" }),
  // insights (Phase 17)
  listInsights: (includeArchived = false) =>
    request<Insight[]>(`/insights?include_archived=${includeArchived}`),
  generateInsight: (insightType = "daily") =>
    request<Insight>("/insights/generate", {
      method: "POST",
      body: JSON.stringify({ insight_type: insightType }),
    }),
  archiveInsight: (id: string) =>
    request<{ insight_id: string; archived: boolean }>(`/insights/${id}/archive`, {
      method: "POST",
    }),
  // cost support (Phase 11 UI)
  listModelCalls: (taskId?: string, limit = 500) =>
    request<ModelCall[]>(
      `/model-calls?limit=${limit}${taskId ? `&task_id=${taskId}` : ""}`,
    ),
  // schedule (Phase 17)
  listScheduledJobs: () => request<ScheduledJob[]>("/schedule"),
  createScheduledJob: (name: string, kind: string, schedule: Record<string, unknown>, payload: Record<string, unknown> = {}) =>
    request<{ job_id: string; name: string; enabled: boolean }>("/schedule", {
      method: "POST",
      body: JSON.stringify({ name, kind, schedule, payload }),
    }),
  deleteScheduledJob: (id: string) =>
    request<void>(`/schedule/${id}`, { method: "DELETE" }),
  // autopilot status (Phase 18 — read-only visibility)
  autopilotStatus: () =>
    request<{ enabled: boolean; killed: boolean; note: string }>("/autopilot/status"),
  // events (resume-from-sequence)
  eventsAfter: (afterSequence: number, limit = 100, sessionId?: string) =>
    request<EventOut[]>(
      `/events?after_sequence=${afterSequence}&limit=${limit}${sessionId ? `&session_id=${encodeURIComponent(sessionId)}` : ""}`,
    ),
  // health
  health: () => request<{ status: string }>("/health"),
  ready: () => request<{ status: string; checks: Record<string, boolean> }>("/ready"),
  // settings
  listSettings: (showSecrets = false) =>
    request<SettingsGroupOut[]>(`/settings?show_secrets=${showSecrets}`),
  getSetting: (key: string, showSecrets = false) =>
    request<SettingOut>(`/settings/${key}?show_secrets=${showSecrets}`),
  setSetting: (key: string, value: string) =>
    request<{ key: string; value: string; source: string }>(`/settings/${key}`, {
      method: "POST",
      body: JSON.stringify({ value }),
    }),
  settingsGroups: () =>
    request<{ groups: Record<string, string>; help: Record<string, string> }>(
      "/settings-groups",
    ),
  // model routing
  listRoutingProviders: () =>
    request<ModelRoutingProviders>("/model-routing/providers"),
  testModelProvider: (provider: string, model?: string, prompt = "ping") =>
    request<ProviderTestResult>("/model-routing/test", {
      method: "POST",
      body: JSON.stringify({ provider, model: model ?? null, prompt }),
    }),
};

/**
 * Event poller with resume-from-sequence: keeps the last seen sequence and
 * only fetches newer events for the specific session.
 */
export class EventPoller {
  private lastSequence = 0;
  private stopped = false;
  private isPolling = false;
  private timer: ReturnType<typeof setInterval> | null = null;

  constructor(
    private onEvents: (events: EventOut[]) => void,
    private onError: (err: unknown) => void,
    private intervalMs = 1500,
    private sessionId?: string,
    initialSequence = 0,
  ) {
    this.lastSequence = initialSequence;
  }

  setSessionId(id?: string) {
    this.sessionId = id;
  }

  start() {
    this.stopped = false;
    const tick = async () => {
      if (this.stopped || this.isPolling) return;
      this.isPolling = true;
      try {
        const events = await api.eventsAfter(this.lastSequence, 100, this.sessionId);
        if (events.length > 0) {
          this.lastSequence = events[events.length - 1].sequence;
          this.onEvents(events);
        }
      } catch (err) {
        if (!this.stopped) this.onError(err);
      } finally {
        this.isPolling = false;
      }
    };
    void tick();
    this.timer = setInterval(tick, this.intervalMs);
  }

  stop() {
    this.stopped = true;
    this.isPolling = false;
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }
}
