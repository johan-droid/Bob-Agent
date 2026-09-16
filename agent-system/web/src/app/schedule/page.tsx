"use client";

/**
 * Schedule tab — real /api/v1/schedule (Phase 17 scheduler persistence).
 * Create + list + delete ride the real endpoints. Manual trigger has no
 * backend endpoint, so it stays honestly disabled (v3.1 §33).
 */

import { useEffect, useState } from "react";
import { api, type ScheduledJob } from "@/lib/api";
import { SkeletonCard } from "@/components/Skeleton";
import { EmptyState } from "@/components/EmptyState";
import { ErrorBanner } from "@/components/ErrorBanner";
import { useToast } from "@/components/Toast";

function describeSchedule(job: ScheduledJob): string {
  const s = job.schedule ?? {};
  if (job.kind === "cron" && typeof s.cron === "string") return `cron: ${s.cron}`;
  if (job.kind === "interval" && s.every != null) return `every ${String(s.every)}s`;
  if (job.kind === "date" && s.run_at != null) {
    const d = new Date(String(s.run_at));
    return Number.isNaN(d.getTime()) ? `at ${String(s.run_at)}` : `at ${d.toLocaleString()}`;
  }
  if (job.kind === "webhook") return "on webhook";
  return JSON.stringify(s);
}

export default function SchedulePage() {
  const [jobs, setJobs] = useState<ScheduledJob[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [name, setName] = useState("");
  const [kind, setKind] = useState("cron");
  const [scheduleText, setScheduleText] = useState('{"cron": "0 9 * * *"}');
  const [scheduleError, setScheduleError] = useState<string | null>(null);
  const toast = useToast();

  const refresh = async () => {
    try {
      setJobs(await api.listScheduledJobs());
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void refresh();
    const t = setInterval(refresh, 8000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const create = async () => {
    if (!name.trim()) {
      setScheduleError("Name is required.");
      return;
    }
    let schedule: Record<string, unknown>;
    try {
      schedule = JSON.parse(scheduleText) as Record<string, unknown>;
      setScheduleError(null);
    } catch {
      setScheduleError("Schedule must be valid JSON (e.g. {\"cron\": \"0 9 * * *\"}).");
      return;
    }
    try {
      await api.createScheduledJob(name.trim(), kind, schedule);
      setName("");
      await refresh();
      toast("Scheduled job created", "success");
    } catch (err) {
      toast((err as Error).message, "error");
    }
  };

  const remove = async (id: string) => {
    if (!window.confirm("Delete this scheduled job?")) return;
    try {
      await api.deleteScheduledJob(id);
      await refresh();
      toast("Scheduled job deleted", "success");
    } catch (err) {
      toast((err as Error).message, "error");
    }
  };

  return (
    <div>
      <h1 className="page-title">Schedule</h1>
      {error && <ErrorBanner message={error} onRetry={() => void refresh()} />}

      <div className="card">
        <h3>New job (POST /api/v1/schedule)</h3>
        <div className="form-row">
          <label htmlFor="sched-name" className="visually-hidden">
            Job name
          </label>
          <input
            id="sched-name"
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Job name"
          />
          <label htmlFor="sched-kind" className="visually-hidden">
            Job kind
          </label>
          <select id="sched-kind" className="input" value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="cron">cron</option>
            <option value="interval">interval</option>
            <option value="date">date</option>
            <option value="webhook">webhook</option>
          </select>
        </div>
        <div className="stack-8">
          <label htmlFor="sched-json" className="muted">
            Schedule JSON
          </label>
          <textarea
            id="sched-json"
            className="json-editor json-editor-short"
            value={scheduleText}
            onChange={(e) => setScheduleText(e.target.value)}
          />
          {scheduleError && (
            <p className="field-error" role="alert">
              {scheduleError}
            </p>
          )}
        </div>
        <div className="form-row">
          <button className="btn" onClick={() => void create()}>
            Create job
          </button>
        </div>
      </div>

      {loading ? (
        <SkeletonCard />
      ) : jobs.length === 0 && !error ? (
        <EmptyState icon="📅" title="No scheduled jobs" hint="Create one with the form above." />
      ) : (
        jobs.map((j) => (
          <div className="card" key={j.job_id}>
            <h3>
              {j.name}{" "}
              <span className="muted">
                ({j.kind} · {j.enabled ? "enabled" : "disabled"})
              </span>
            </h3>
            <p>{describeSchedule(j)}</p>
            <code className="muted">{JSON.stringify(j.schedule)}</code>
            <p className="disabled-note">
              Next/last run are not exposed by GET /api/v1/schedule yet — shown from the stored
              schedule config only.
            </p>
            <div className="approval-actions">
              <span title="Backend API pending: no manual-trigger endpoint yet">
                <button className="btn secondary" disabled aria-disabled="true">
                  Trigger now
                </button>
              </span>
              <button className="btn danger" onClick={() => void remove(j.job_id)}>
                Delete
              </button>
            </div>
          </div>
        ))
      )}
    </div>
  );
}
