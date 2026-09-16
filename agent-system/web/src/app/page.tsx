"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import { Skeleton } from "@/components/Skeleton";
import { ErrorBanner } from "@/components/ErrorBanner";
import { useToast } from "@/components/Toast";

export default function DashboardPage() {
  const [health, setHealth] = useState<string>("checking…");
  const [checks, setChecks] = useState<Record<string, boolean>>({});
  const [taskCounts, setTaskCounts] = useState<Record<string, number>>({});
  const [sessionCount, setSessionCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const toast = useToast();

  const refresh = async () => {
    try {
      const h = await api.health();
      const r = await api.ready();
      setHealth(h.status);
      setChecks(r.checks);
      const tasks = await api.listTasks();
      const counts: Record<string, number> = {};
      for (const t of tasks) counts[t.state] = (counts[t.state] ?? 0) + 1;
      setTaskCounts(counts);
      const sessions = await api.listSessions();
      setSessionCount(sessions.length);
      setError(null);
    } catch (err) {
      const msg = (err as Error).message;
      setHealth(`unreachable (${msg})`);
      setError(msg);
      toast(msg, "error");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void refresh();
    const t = setInterval(refresh, 15000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div>
      <h1 className="page-title">System Overview</h1>

      {/* Quick Actions */}
      <div className="quick-actions">
        <Link href="/chat" className="quick-action">
          <span className="quick-action-icon">💬</span>
          <div>
            <div className="quick-action-text">Launch Chat</div>
            <div className="quick-action-sub">ChatGPT-style interface</div>
          </div>
        </Link>
        <Link href="/kanban" className="quick-action">
          <span className="quick-action-icon">📊</span>
          <div>
            <div className="quick-action-text">Kanban Board</div>
            <div className="quick-action-sub">Manage tasks</div>
          </div>
        </Link>
        <Link href="/workspace" className="quick-action">
          <span className="quick-action-icon">🖥️</span>
          <div>
            <div className="quick-action-text">Workspace</div>
            <div className="quick-action-sub">Browse files</div>
          </div>
        </Link>
        <Link href="/settings" className="quick-action">
          <span className="quick-action-icon">🔧</span>
          <div>
            <div className="quick-action-text">Settings</div>
            <div className="quick-action-sub">Configure providers</div>
          </div>
        </Link>
        <Link href="/cost" className="quick-action">
          <span className="quick-action-icon">💰</span>
          <div>
            <div className="quick-action-text">Cost Tracker</div>
            <div className="quick-action-sub">Monitor spending</div>
          </div>
        </Link>
        <Link href="/approvals" className="quick-action">
          <span className="quick-action-icon">✅</span>
          <div>
            <div className="quick-action-text">Approvals</div>
            <div className="quick-action-sub">Review requests</div>
          </div>
        </Link>
      </div>

      {error && <ErrorBanner message={error} onRetry={() => void refresh()} />}

      {/* Stats cards */}
      {loading ? (
        <div className="stats-grid">
          <Skeleton lines={4} />
          <Skeleton lines={4} />
          <Skeleton lines={3} />
        </div>
      ) : (
        <div className="stats-grid">
          <div className="card">
            <h3>Backend status</h3>
            <p className="muted">Health: {health}</p>
            {Object.entries(checks).map(([k, v]) => (
              <p key={k} className="muted">
                {k}: {v ? "✅" : "❌"}
              </p>
            ))}
          </div>
          <div className="card">
            <h3>Task queue</h3>
            {Object.entries(taskCounts).length === 0 ? (
              <p className="muted">No tasks yet.</p>
            ) : (
              Object.entries(taskCounts).map(([state, n]) => (
                <p key={state}>
                  <span className={`state-${state}`}>{state}</span>: {n}
                </p>
              ))
            )}
          </div>
          <div className="card">
            <h3>Sessions</h3>
            <p className="stat-number">{sessionCount}</p>
            <p className="muted">Total conversations</p>
          </div>
        </div>
      )}
    </div>
  );
}
