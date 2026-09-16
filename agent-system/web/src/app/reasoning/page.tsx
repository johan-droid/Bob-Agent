"use client";

/**
 * Reasoning (= Decision & Execution Trace) tab — real /api/v1/recordings +
 * replay in INSPECT mode (Phase 15). Auditable events only, no fabricated
 * token streams (v3.1 §2 reconciliation).
 */

import { useEffect, useState } from "react";
import { api, type Recording, type ReplayResult } from "@/lib/api";
import { SkeletonCard } from "@/components/Skeleton";
import { EmptyState } from "@/components/EmptyState";
import { ErrorBanner } from "@/components/ErrorBanner";
import { LoadingSpinner } from "@/components/LoadingSpinner";
import { useToast } from "@/components/Toast";

export default function ReasoningTracePage() {
  const [recordings, setRecordings] = useState<Recording[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [replay, setReplay] = useState<ReplayResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [inspecting, setInspecting] = useState(false);
  const toast = useToast();

  const refresh = async () => {
    try {
      setRecordings(await api.listRecordings());
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const inspect = async (id: string) => {
    setSelected(id);
    setReplay(null);
    setInspecting(true);
    try {
      // INSPECT is always safe: read-only, no approvals needed.
      setReplay(await api.replay(id, "INSPECT"));
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setInspecting(false);
    }
  };

  return (
    <div>
      <h1 className="page-title">Reasoning Trace</h1>
      {error && <ErrorBanner message={error} onRetry={() => void refresh()} />}
      {loading ? (
        <SkeletonCard />
      ) : recordings.length === 0 && !error ? (
        <EmptyState
          icon="🔍"
          title="No recordings yet"
          hint="Agent runs recorded via RecordingContext appear here."
        />
      ) : (
        recordings.map((r) => (
          <button
            type="button"
            className="card clickable"
            key={r.recording_id}
            onClick={() => void inspect(r.recording_id)}
            aria-pressed={selected === r.recording_id}
          >
            <h3>
              {r.recording_id.slice(0, 18)}…{" "}
              <span className="muted">
                ({r.action_count} steps · {r.finished ? "complete" : "recording"})
              </span>
            </h3>
            <p className="muted">
              session {r.session_id?.slice(0, 8) ?? "—"} · started{" "}
              {new Date(r.started).toLocaleString()}
            </p>
          </button>
        ))
      )}
      {inspecting && <LoadingSpinner label="Replaying in INSPECT mode…" />}
      {replay && (
        <div className="card">
          <h3>
            Trace {replay.recording_id.slice(0, 18)}…{" "}
            <span className={replay.fingerprint_match ? "" : "muted"}>
              {replay.fingerprint_match ? "· environment matches" : "· environment differs from recording"}
            </span>
          </h3>
          <p className="muted">
            mode {replay.mode} · allowed {replay.allowed ? "yes" : "no"} · side effects{" "}
            {replay.side_effects}
            {replay.blocked_reason ? ` · blocked: ${replay.blocked_reason}` : ""}
          </p>
          <ol className="steps-timeline">
            {replay.steps.map((s) => (
              <li key={s.index}>
                <span className="step-kind">#{s.index} {s.kind}</span>
                {s.name}
                <code>
                  {new Date(s.timestamp).toLocaleTimeString()} ·{" "}
                  {JSON.stringify(s.payload).slice(0, 300)}
                </code>
              </li>
            ))}
          </ol>
        </div>
      )}
    </div>
  );
}
