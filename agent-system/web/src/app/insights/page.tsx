"use client";

/**
 * Insights tab — real /api/v1/insights (Phase 17). Insights derive only from
 * canonical events; the UI never fabricates content (v3.1 §33). Stored HTML
 * is rendered escaped so markup can never execute.
 */

import { useEffect, useState } from "react";
import { api, type Insight } from "@/lib/api";
import { SkeletonCard } from "@/components/Skeleton";
import { EmptyState } from "@/components/EmptyState";
import { ErrorBanner } from "@/components/ErrorBanner";
import { useToast } from "@/components/Toast";

export default function InsightsPage() {
  const [insights, setInsights] = useState<Insight[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const toast = useToast();

  const refresh = async () => {
    try {
      setInsights(await api.listInsights(showArchived));
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void refresh();
    const t = setInterval(refresh, 10000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showArchived]);

  const generate = async () => {
    setGenerating(true);
    try {
      await api.generateInsight("daily");
      await refresh();
      toast("Daily insight generated", "success");
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setGenerating(false);
    }
  };

  const archive = async (id: string) => {
    try {
      await api.archiveInsight(id);
      await refresh();
      toast("Insight archived", "success");
    } catch (err) {
      toast((err as Error).message, "error");
    }
  };

  return (
    <div>
      <div className="page-head">
        <h1 className="page-title">Insights</h1>
        <div className="toolbar">
          <label htmlFor="insights-archived" className="muted">
            <input
              id="insights-archived"
              type="checkbox"
              checked={showArchived}
              onChange={(e) => setShowArchived(e.target.checked)}
            />{" "}
            Include archived
          </label>
          <button className="btn" disabled={generating} onClick={() => void generate()}>
            {generating ? "Generating…" : "Generate daily insight"}
          </button>
        </div>
      </div>
      {error && <ErrorBanner message={error} onRetry={() => void refresh()} />}
      {loading ? (
        <SkeletonCard />
      ) : insights.length === 0 && !error ? (
        <EmptyState
          icon="📊"
          title="No insights yet"
          hint="Generate one from the canonical event stream."
        />
      ) : (
        insights.map((i) => (
          <div className="card" key={i.insight_id}>
            <h3>
              {i.insight_type.toUpperCase()} · {new Date(i.generated_at).toLocaleString()}
              {i.archived && <span className="muted"> · archived</span>}
            </h3>
            <ul>
              {i.key_findings.map((f, idx) => (
                <li key={idx}>
                  [{f.kind}] {f.finding}
                  {f.suggested_action && (
                    <span className="muted"> — {f.suggested_action}</span>
                  )}
                </li>
              ))}
            </ul>
            {i.content && (
              <details>
                <summary className="muted">Full content (rendered safely as text)</summary>
                <pre className="preview-pre">{i.content}</pre>
              </details>
            )}
            {!i.archived && (
              <div className="stack-8">
                <button className="btn secondary" onClick={() => void archive(i.insight_id)}>
                  Archive
                </button>
              </div>
            )}
          </div>
        ))
      )}
    </div>
  );
}
