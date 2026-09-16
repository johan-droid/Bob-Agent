"use client";

import { useEffect, useMemo, useState } from "react";
import { api, type EventOut, type Task } from "@/lib/api";
import { SkeletonCard } from "@/components/Skeleton";
import { EmptyState } from "@/components/EmptyState";
import { ErrorBanner } from "@/components/ErrorBanner";
import { useToast } from "@/components/Toast";

const COLUMNS: { state: string; label: string }[] = [
  { state: "PENDING", label: "Pending" },
  { state: "QUEUED", label: "Queued" },
  { state: "RUNNING", label: "In Progress" },
  { state: "REVIEW", label: "Review" },
  { state: "SUCCEEDED", label: "Done" },
  { state: "FAILED", label: "Failed" },
];

// Mirrors backend TRANSITIONS (domain/tasks.py). Moves call the real
// POST /tasks/{id}/transition; invalid ones surface the 409 detail.
const ALLOWED_TARGETS: Record<string, string[]> = {
  PENDING: ["PLANNING", "QUEUED", "CANCELLED"],
  PLANNING: ["QUEUED", "FAILED", "CANCELLED"],
  QUEUED: ["RUNNING", "CANCELLED"],
  RUNNING: ["BLOCKED_APPROVAL", "RECOVERING", "REVIEW", "SUCCEEDED", "FAILED", "CANCELLED"],
  BLOCKED_APPROVAL: ["RUNNING", "FAILED", "CANCELLED"],
  RECOVERING: ["QUEUED", "RUNNING", "FAILED", "CANCELLED"],
  REVIEW: ["SUCCEEDED", "FAILED", "RUNNING"],
  SUCCEEDED: [],
  FAILED: ["QUEUED", "RECOVERING"],
  CANCELLED: [],
};

type SortKey = "state" | "title" | "attempt";

export default function KanbanPage() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [agentFilter, setAgentFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<SortKey>("state");
  const [selected, setSelected] = useState<Task | null>(null);
  const [taskEvents, setTaskEvents] = useState<EventOut[]>([]);
  const [moveTarget, setMoveTarget] = useState("");
  const toast = useToast();

  const refresh = async () => {
    try {
      setTasks(await api.listTasks());
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void refresh();
    const t = setInterval(refresh, 3000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const agents = useMemo(
    () => [...new Set(tasks.map((t) => t.agent_type ?? "unassigned"))].sort(),
    [tasks],
  );

  const filtered = useMemo(() => {
    let rows = tasks;
    if (agentFilter !== "all") {
      rows = rows.filter((t) => (t.agent_type ?? "unassigned") === agentFilter);
    }
    if (statusFilter !== "all") {
      rows = rows.filter((t) => t.state === statusFilter);
    }
    const q = search.trim().toLowerCase();
    if (q) {
      rows = rows.filter(
        (t) =>
          t.title.toLowerCase().includes(q) ||
          t.id.toLowerCase().includes(q) ||
          (t.task_type ?? "").toLowerCase().includes(q),
      );
    }
    const sorted = [...rows];
    if (sort === "title") sorted.sort((a, b) => a.title.localeCompare(b.title));
    else if (sort === "attempt") sorted.sort((a, b) => b.attempt - a.attempt);
    return sorted;
  }, [tasks, agentFilter, statusFilter, search, sort]);

  const otherTasks = filtered.filter((t) => !COLUMNS.some((c) => c.state === t.state));

  const openCard = async (t: Task) => {
    setSelected(t);
    setMoveTarget("");
    setTaskEvents([]);
    try {
      const events = await api.eventsAfter(0, 1000);
      setTaskEvents(events.filter((e) => e.task_id === t.id).slice(-20));
    } catch {
      setTaskEvents([]);
    }
  };

  const retry = async (id: string) => {
    try {
      await api.retryTask(id);
      await refresh();
      toast("Retry scheduled", "success");
    } catch (err) {
      toast((err as Error).message, "error");
    }
  };

  const move = async () => {
    if (!selected || !moveTarget) return;
    try {
      await api.transitionTask(selected.id, moveTarget, "operator move from kanban");
      toast(`Moved to ${moveTarget}`, "success");
      setSelected(null);
      await refresh();
    } catch (err) {
      toast((err as Error).message, "error");
    }
  };

  return (
    <div>
      <h1 className="page-title">Kanban</h1>
      {error && <ErrorBanner message={error} onRetry={() => void refresh()} />}

      <div className="toolbar" role="search">
        <label htmlFor="kanban-search" className="visually-hidden">
          Search tasks
        </label>
        <input
          id="kanban-search"
          className="input"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search title, id, type…"
        />
        <label htmlFor="kanban-agent" className="muted">
          Agent
        </label>
        <select id="kanban-agent" value={agentFilter} onChange={(e) => setAgentFilter(e.target.value)}>
          <option value="all">All</option>
          {agents.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>
        <label htmlFor="kanban-status" className="muted">
          Status
        </label>
        <select id="kanban-status" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          <option value="all">All</option>
          {COLUMNS.map((c) => (
            <option key={c.state} value={c.state}>
              {c.label}
            </option>
          ))}
        </select>
        <label htmlFor="kanban-sort" className="muted">
          Sort
        </label>
        <select id="kanban-sort" value={sort} onChange={(e) => setSort(e.target.value as SortKey)}>
          <option value="state">By column</option>
          <option value="title">By title</option>
          <option value="attempt">By attempts</option>
        </select>
      </div>

      {loading ? (
        <div className="kanban">
          {COLUMNS.map((c) => (
            <div className="column" key={c.state}>
              <h3>{c.label}</h3>
              <SkeletonCard />
            </div>
          ))}
        </div>
      ) : filtered.length === 0 && !error ? (
        <EmptyState icon="📊" title="No tasks match" hint="Adjust filters or create a session from Chat." />
      ) : (
        <div className="kanban">
          {COLUMNS.map(({ state, label }) => {
            const items = filtered.filter((t) => t.state === state);
            return (
              <div className="column" key={state}>
                <h3>
                  {label} ({items.length})
                </h3>
                {items.map((t) => (
                  <div className="card clickable" key={t.id} onClick={() => void openCard(t)} role="button" tabIndex={0}
                    onKeyDown={(e) => { if (e.key === "Enter") void openCard(t); }}
                    aria-label={`Task ${t.title}`}>
                    <h3>{t.title}</h3>
                    <p className="muted">
                      {t.agent_type ?? "unassigned"} · attempt {t.attempt}
                    </p>
                    {t.depends_on.length > 0 && (
                      <p>
                        {t.depends_on.map((d) => (
                          <span key={d} className="dep-badge" title={`Depends on ${d}`}>
                            ⛓ {d.slice(0, 8)}
                          </span>
                        ))}
                      </p>
                    )}
                    {t.last_error && <p className="muted">⚠ {t.last_error}</p>}
                    {state === "FAILED" && (
                      <button
                        className="btn secondary"
                        onClick={(e) => { e.stopPropagation(); void retry(t.id); }}
                      >
                        Retry
                      </button>
                    )}
                  </div>
                ))}
              </div>
            );
          })}
          {otherTasks.length > 0 && (
            <div className="column" key="OTHER">
              <h3>Other ({otherTasks.length})</h3>
              {otherTasks.map((t) => (
                <div className="card clickable" key={t.id} onClick={() => void openCard(t)} role="button" tabIndex={0}
                  onKeyDown={(e) => { if (e.key === "Enter") void openCard(t); }}>
                  <h3>{t.title}</h3>
                  <p className="muted">
                    {t.state} · {t.agent_type ?? "unassigned"} · attempt {t.attempt}
                  </p>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {selected && (
        <div className="modal-backdrop" onClick={() => setSelected(null)}>
          <div className="modal" role="dialog" aria-modal="true" aria-label={`Task ${selected.title}`} onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>{selected.title}</h2>
              <button className="modal-close" onClick={() => setSelected(null)} aria-label="Close">
                ×
              </button>
            </div>
            <p className="muted">
              {selected.id} · {selected.task_type} · {selected.agent_type ?? "unassigned"} · attempt{" "}
              {selected.attempt}
            </p>
            <p>
              State: <span className={`state-${selected.state}`}>{selected.state}</span>
            </p>
            <h3>Dependencies</h3>
            {selected.depends_on.length === 0 ? (
              <p className="muted">No dependencies.</p>
            ) : (
              <p>
                {selected.depends_on.map((d) => (
                  <span key={d} className="dep-badge">
                    ⛓ {d}
                  </span>
                ))}
              </p>
            )}
            {selected.last_error && (
              <>
                <h3>Last error</h3>
                <p className="muted">⚠ {selected.last_error}</p>
              </>
            )}
            <h3>Recent events</h3>
            {taskEvents.length === 0 ? (
              <p className="muted">No linked events found.</p>
            ) : (
              <ul>
                {taskEvents.map((e) => (
                  <li key={e.event_id} className="muted">
                    #{e.sequence} {e.type} · {e.actor} · {new Date(e.timestamp).toLocaleString()}
                  </li>
                ))}
              </ul>
            )}
            <h3>Move (real state transition)</h3>
            {(ALLOWED_TARGETS[selected.state] ?? []).length === 0 ? (
              <p className="muted">Terminal state — no moves allowed. Use Retry for failed tasks.</p>
            ) : (
              <div className="form-row">
                <label htmlFor="kanban-move" className="visually-hidden">
                  Target state
                </label>
                <select id="kanban-move" className="input" value={moveTarget} onChange={(e) => setMoveTarget(e.target.value)}>
                  <option value="">Select target…</option>
                  {(ALLOWED_TARGETS[selected.state] ?? []).map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
                <button className="btn" disabled={!moveTarget} onClick={() => void move()}>
                  Move
                </button>
              </div>
            )}
            {selected.state === "FAILED" && (
              <div className="stack-8">
                <button className="btn secondary" onClick={() => { void retry(selected.id); setSelected(null); }}>
                  Retry task
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
